#!/usr/bin/env python3
"""Create or verify the unprivileged V2 Task 14 review package."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from build_r4_protected_candidate import (
    AUTHORITY_ID,
    CANONICAL_REQUIRED_BLOB_PATHS,
    NAMESPACE,
    _canonical,
    _domain_sha,
    _git_evidence,
    _sha,
    _v2_document_digest,
    validate_required_blob_rows,
)
from r4_protected_fixed_git import GitRunner, run_fixed_git, run_recorded_git
from mlb_backtest.secure_fs import open_trusted_root

HELPER_TEAM_ID = "3BMF4LM6TM"
HELPER_SIGNING_IDENTITY_SHA1 = "4C6503D0A78A9F51C86CC7C9683115E74A4902A7"
TASK14_APPROVAL_VERIFICATION_PUBLIC_KEY_SHA256 = (
    "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1"
)
PROVISIONER_SOURCE_ROOT = (
    "tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner"
)
PROVISIONER_SWIFT_SOURCE_DOMAIN = "R4V2_PROVISIONER_SWIFT_SOURCES\0"
# These bounds follow the V2 contract: canonical JSON documents and reviewed Swift
# sources are intentionally small; an artifact is bounded by its manifest size.
MAX_REVIEW_PACKAGE_BYTES = 64 * 1024
MAX_CANONICAL_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_MANIFEST_BYTES = MAX_CANONICAL_DOCUMENT_BYTES
MAX_LOCK_BYTES = MAX_CANONICAL_DOCUMENT_BYTES
MAX_POLICY_BYTES = MAX_CANONICAL_DOCUMENT_BYTES
MAX_SWIFT_SOURCE_BYTES = 4 * 1024 * 1024
# The sealed policy fixes archive.max_members=100000 and
# archive.max_unpacked_bytes=8589934592.  These transport-side limits preserve
# that contract while keeping validation streaming rather than memory-bound.
MAX_TRANSPORT_ROWS = 100_000
MAX_TRANSPORT_PATH_DEPTH = 128
MAX_TRANSPORT_BYTES = 8_589_934_592
MAX_CHECKOUT_ENTRY_BYTES = MAX_TRANSPORT_BYTES
MAX_ARTIFACT_BYTES = MAX_TRANSPORT_BYTES
MAX_CHECKOUT_TOTAL_BYTES = MAX_TRANSPORT_BYTES
MAX_ARTIFACT_TOTAL_BYTES = MAX_TRANSPORT_BYTES
MAX_EXACT_SUITE_LOG_BYTES = 16 * 1024 * 1024
MAX_HELPER_BINARY_BYTES = 128 * 1024 * 1024
MAX_HELPER_REQUIREMENT_BYTES = 64 * 1024
CANONICAL_PROVISIONER_SWIFT_SOURCE_PATHS = tuple(
    f"{PROVISIONER_SOURCE_ROOT}/{name}"
    for name in (
        "Adapters.swift",
        "Admission.swift",
        "Archive.swift",
        "Bootstrap.swift",
        "BundleTraversal.swift",
        "CLI.swift",
        "CanonicalJSON.swift",
        "Constants.swift",
        "Keyring.swift",
        "MachO.swift",
        "Materialize.swift",
        "Models.swift",
        "POSIX.swift",
        "ProcessRunner.swift",
        "Publication.swift",
        "SignalMonitor.swift",
        "StateMachine.swift",
        "Storage.swift",
        "main.swift",
    )
)


_TASK2C_ALLOWED_IMPORT_ROOTS = {
    "argparse",
    "ast",
    "collections",
    "__future__",
    "hashlib",
    "json",
    "mlb_backtest",
    "os",
    "pathlib",
    "re",
    "r4_protected_fixed_git",
    "stat",
    "typing",
    "build_r4_protected_candidate",
}
_TASK2C_FORBIDDEN_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "hasattr",
    "input",
    "locals",
    "memoryview",
    "setattr",
    "vars",
}
_TASK2C_FORBIDDEN_ATTRIBUTES = {
    "CDLL",
    "PyDLL",
    "WinDLL",
    "check_call",
    "check_output",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "fork",
    "popen",
    "posix_spawn",
    "posix_spawnp",
    "run",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "system",
}
_TASK2C_ALLOWED_FROM_IMPORTS = {
    "__future__": {"annotations"},
    "collections.abc": {"Sequence"},
    "pathlib": {"Path"},
    "typing": {"Any"},
    "build_r4_protected_candidate": {
        "AUTHORITY_ID",
        "CANONICAL_REQUIRED_BLOB_PATHS",
        "NAMESPACE",
        "_canonical",
        "_domain_sha",
        "_git_evidence",
        "_sha",
        "_v2_document_digest",
        "validate_required_blob_rows",
    },
    "r4_protected_fixed_git": {"GitRunner", "run_fixed_git", "run_recorded_git"},
    "mlb_backtest.secure_fs": {"open_trusted_root"},
}


def validate_task2c_ast_policy(tree: ast.AST) -> None:
    """Reject imports/reflection/native/process routes outside Task2c's fixed Git seam."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.asname
                or alias.name.split(".")[0] not in _TASK2C_ALLOWED_IMPORT_ROOTS
                for alias in node.names
            ):
                raise ValueError("Task2c imports a forbidden dependency")
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level
                or not node.module
                or any(alias.name == "*" or alias.asname for alias in node.names)
                or node.module not in _TASK2C_ALLOWED_FROM_IMPORTS
                or {alias.name for alias in node.names}
                - _TASK2C_ALLOWED_FROM_IMPORTS[node.module]
            ):
                raise ValueError("Task2c imports a forbidden dependency")
        elif isinstance(node, ast.Name) and node.id in (
            _TASK2C_FORBIDDEN_NAMES | _TASK2C_FORBIDDEN_ATTRIBUTES
        ):
            raise ValueError("Task2c uses dynamic reflection or evaluation")
        elif isinstance(node, ast.Attribute) and node.attr in _TASK2C_FORBIDDEN_ATTRIBUTES:
            raise ValueError("Task2c uses a native or process execution primitive")
        elif isinstance(node, (ast.Name, ast.Attribute)):
            identifier = node.id if isinstance(node, ast.Name) else node.attr
            if isinstance(node, ast.Attribute) and (
                identifier.startswith("__") or identifier.endswith("__")
            ):
                raise ValueError("Task2c uses dynamic attribute dispatch")
            if "key" + "b" in identifier.replace("_", "").replace("-", "").lower():
                raise ValueError("Task2c must not name the receipt-signing authority")


def _read_private_descriptor(descriptor: int, label: str, maximum_bytes: int) -> bytes:
    """Read a known-private regular descriptor, bounded and stable throughout."""
    maximum_bytes = _require_bounded_nonnegative_int(
        maximum_bytes, f"{label} maximum size", MAX_TRANSPORT_BYTES
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size < 0
        or opened.st_size > maximum_bytes
    ):
        raise ValueError(f"{label} is not a bounded private regular file")
    remaining = opened.st_size
    chunks: list[bytes] = []
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise ValueError(f"{label} changed during read")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise ValueError(f"{label} exceeded its declared size")
    return b"".join(chunks)


def _private_bytes(path: Path, maximum_bytes: int, label: str) -> bytes:
    maximum_bytes = _require_bounded_nonnegative_int(
        maximum_bytes, f"{label} maximum size", MAX_TRANSPORT_BYTES
    )
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError(f"{label} must be an absolute named file")
    parent_path = path.parent.resolve(strict=True)
    parent = os.open(
        parent_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        details = os.fstat(descriptor)
        payload = _read_private_descriptor(descriptor, label, maximum_bytes)
        after = os.fstat(descriptor)
        rebound = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        identity = (details.st_dev, details.st_ino, details.st_nlink, details.st_size)
        if any(
            (item.st_dev, item.st_ino, item.st_nlink, item.st_size) != identity
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            for item in (after, rebound)
        ):
            raise ValueError(f"{label} rebound during read")
        if len(payload) != details.st_size:
            raise ValueError(f"{label} changed during read")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_bounded_nonnegative_int(value: object, label: str, maximum: int) -> int:
    """Accept only JSON integers within a closed nonnegative contract."""
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError(f"{label} is not a bounded nonnegative integer")
    return value


def _manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _private_bytes(path, MAX_BUNDLE_MANIFEST_BYTES, "bundle manifest")
    return _manifest_bytes(raw), raw


def _manifest_bytes(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("bundle manifest is not JSON") from exc
    if not isinstance(value, dict) or raw not in {_canonical(value), _canonical(value) + b"\n"}:
        raise ValueError("bundle manifest is not canonical")
    if (
        value.get("kind") != "R4_RUNTIME_INPUT_BUNDLE_V2"
        or value.get("namespace") != NAMESPACE
        or value.get("authority_id") != AUTHORITY_ID
    ):
        raise ValueError("bundle is not a V2 transport manifest")
    if value.get("sha256") != _sha(
        _canonical({key: item for key, item in value.items() if key != "sha256"})
    ):
        raise ValueError("bundle manifest self digest mismatch")
    return value


REVIEW_PACKAGE_FIELDS = {
    "kind",
    "namespace",
    "authority_id",
    "reviewed_head",
    "reviewed_tree",
    "required_blob_set_sha256",
    "exact_suite_log_sha256",
    "input_bundle_manifest_sha256",
    "lock_sha256",
    "python_archive_sha256",
    "wheel_set_sha256",
    "materialization_policy_sha256",
    "provisioner_swift_source_sha256",
    "provisioner_binary_sha256",
    "provisioner_designated_requirement_sha256",
    "helper_hashes",
    "helper_team_id",
    "helper_signing_identity_sha1",
    "task14_approval_verification_public_key_sha256",
    "sha256",
}


def _validate_review_package(value: object, raw: bytes) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REVIEW_PACKAGE_FIELDS:
        raise ValueError("review package has unexpected fields")
    if raw not in {_canonical(value), _canonical(value) + b"\n"}:
        raise ValueError("review package is not canonical")
    if (
        value.get("kind") != "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2"
        or value.get("namespace") != NAMESPACE
        or value.get("authority_id") != AUTHORITY_ID
        or value.get("helper_team_id") != HELPER_TEAM_ID
        or value.get("helper_signing_identity_sha1") != HELPER_SIGNING_IDENTITY_SHA1
        or value.get("task14_approval_verification_public_key_sha256")
        != TASK14_APPROVAL_VERIFICATION_PUBLIC_KEY_SHA256
    ):
        raise ValueError("review package has invalid fixed evidence")
    if value.get("sha256") != _sha(
        _canonical({key: item for key, item in value.items() if key != "sha256"})
    ):
        raise ValueError("review package self digest mismatch")
    if not all(
        isinstance(value.get(key), str)
        and len(value[key]) == 64
        and all(character in "0123456789abcdef" for character in value[key])
        for key in REVIEW_PACKAGE_FIELDS
        - {"kind", "namespace", "authority_id", "reviewed_head", "reviewed_tree", "helper_hashes", "helper_team_id", "helper_signing_identity_sha1"}
    ):
        raise ValueError("review package digest is malformed")
    if not all(
        isinstance(value.get(key), str)
        and len(value[key]) == 40
        and all(character in "0123456789abcdef" for character in value[key])
        for key in ("reviewed_head", "reviewed_tree")
    ):
        raise ValueError("review package Git evidence is malformed")
    helpers = value.get("helper_hashes")
    if (
        not isinstance(helpers, list)
        or helpers
        != [
            {"path": "bootstrap/provisioner", "sha256": value["provisioner_binary_sha256"]},
            {
                "path": "bootstrap/provisioner.codesign-requirement.txt",
                "sha256": value["provisioner_designated_requirement_sha256"],
            },
        ]
    ):
        raise ValueError("review package helper evidence is malformed")
    return value


def verify_task14_review_package_v2(review_package: Path) -> dict[str, Any]:
    """Read and validate a pre-existing canonical review package without rebuilding it."""
    raw = _private_bytes(
        review_package, MAX_REVIEW_PACKAGE_BYTES, "review package"
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("review package is not JSON") from exc
    return _validate_review_package(value, raw)


def _required_rows(
    source: Path, runner: GitRunner
) -> tuple[str, str, list[dict[str, str]], list[tuple[str, str, str]]]:
    head, tree, records = _git_evidence(source, runner)
    blobs = {path: blob for path, _, blob in records}
    if set(CANONICAL_REQUIRED_BLOB_PATHS) - set(blobs):
        raise ValueError("review source misses canonical required paths")
    rows = []
    for path in CANONICAL_REQUIRED_BLOB_PATHS:
        payload = run_fixed_git(
            source, ("cat-file", "blob", blobs[path]), runner=runner
        )
        rows.append({"path": path, "blob": blobs[path], "sha256": _sha(payload)})
    validate_required_blob_rows(rows)
    return head, tree, rows, records


def _swift_tokens(text: str) -> list[tuple[str, str]]:
    """Tokenize the Swift subset needed to locate the closed Constants declaration."""
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = length if end < 0 else end + 1
        elif text.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if text.startswith("/*", index):
                    depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError("reviewed Swift Constants source has an unterminated comment")
        elif character == '"':
            multiline = text.startswith('\"\"\"', index)
            start = index + (3 if multiline else 1)
            end_marker = '\"\"\"' if multiline else '"'
            end = start
            while end < length:
                if text.startswith(end_marker, end):
                    break
                if not multiline and text[end] == "\\":
                    end += 2
                else:
                    end += 1
            if end >= length:
                raise ValueError("reviewed Swift Constants source has an unterminated string")
            tokens.append(("string" if not multiline else "multiline_string", text[start:end]))
            index = end + len(end_marker)
        elif character == "#" and index + 1 < length and text[index + 1] in {'#', '"'}:
            hashes = 0
            while index + hashes < length and text[index + hashes] == "#":
                hashes += 1
            if index + hashes >= length or text[index + hashes] != '"':
                tokens.append(("punctuation", character))
                index += 1
                continue
            quote_index = index + hashes
            multiline = text.startswith('\"\"\"', quote_index)
            start = quote_index + (3 if multiline else 1)
            marker = ('\"\"\"' if multiline else '"') + ("#" * hashes)
            end = text.find(marker, start)
            if end < 0:
                raise ValueError("reviewed Swift Constants source has an unterminated raw string")
            tokens.append(("raw_multiline_string" if multiline else "raw_string", text[start:end]))
            index = end + len(marker)
        elif character.isascii() and (character.isalpha() or character == "_"):
            end = index + 1
            while end < length and text[end].isascii() and (
                text[end].isalnum() or text[end] == "_"
            ):
                end += 1
            tokens.append(("identifier", text[index:end]))
            index = end
        else:
            tokens.append(("punctuation", character))
            index += 1
    return tokens


def _reviewed_swift_key_a_pin(source: Path, records: list[tuple[str, str, str]]) -> str:
    constants = source / PROVISIONER_SOURCE_ROOT / "Constants.swift"
    payload = _private_bytes(constants, MAX_SWIFT_SOURCE_BYTES, "reviewed Swift Constants source")
    try:
        tokens = _swift_tokens(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("reviewed Swift Constants source is not UTF-8") from exc
    if any(
        value == "#"
        and index + 1 < len(tokens)
        and tokens[index + 1][1] in {"if", "elseif", "else", "endif"}
        for index, (_, value) in enumerate(tokens)
    ):
        raise ValueError("reviewed Swift Constants source has conditional compilation")
    enum_starts = [
        index
        for index in range(len(tokens) - 3)
        if [value for _, value in tokens[index : index + 4]]
        == ["public", "enum", "ProvisionerConstants", "{"]
    ]
    if len(enum_starts) != 1:
        raise ValueError("reviewed Swift Constants source has no unique Constants enum")
    body_start = enum_starts[0] + 3
    depth = 0
    body_end: int | None = None
    for index in range(body_start, len(tokens)):
        value = tokens[index][1]
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                body_end = index
                break
    if body_end is None:
        raise ValueError("reviewed Swift Constants enum is unterminated")
    declarations: list[tuple[int, str]] = []
    depth = 0
    index = body_start + 1
    expected = [
        "public",
        "static",
        "let",
        "task14ApprovalVerificationPublicKeySHA256",
        "=",
    ]
    while index < body_end:
        value = tokens[index][1]
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
        elif depth == 0 and [item for _, item in tokens[index : index + 5]] == expected:
            if index + 5 >= body_end or tokens[index + 5][0] != "string":
                raise ValueError("reviewed Swift Key-A pin is not a literal declaration")
            pin = tokens[index + 5][1]
            if index + 6 < body_end and tokens[index + 6][1] == "+":
                raise ValueError("reviewed Swift Key-A pin must be a single literal")
            declarations.append((index, pin))
            index += 5
        index += 1
    all_declarations: list[tuple[int, str]] = []
    for index in range(len(tokens) - 5):
        if [item for _, item in tokens[index : index + 5]] != expected:
            continue
        if tokens[index + 5][0] != "string":
            raise ValueError("reviewed Swift Key-A pin is not a literal declaration")
        if index + 6 < len(tokens) and tokens[index + 6][1] == "+":
            raise ValueError("reviewed Swift Key-A pin must be a single literal")
        all_declarations.append((index, tokens[index + 5][1]))
    if (
        len(declarations) != 1
        or len(all_declarations) != 1
        or declarations != all_declarations
        or not _is_lower_hex(declarations[0][1], 64)
        or declarations[0][1] != TASK14_APPROVAL_VERIFICATION_PUBLIC_KEY_SHA256
    ):
        raise ValueError("reviewed Swift Key-A pin is not the closed compile-time value")
    expected_path = f"{PROVISIONER_SOURCE_ROOT}/Constants.swift"
    expected_blob = next((blob for path, _, blob in records if path == expected_path), None)
    if expected_blob is None:
        raise ValueError("reviewed Swift Constants source is missing")
    git_blob = hashlib.sha1(
        b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    ).hexdigest()
    if git_blob != expected_blob:
        raise ValueError("reviewed Swift Constants source differs from Git evidence")
    return declarations[0][1]


def _swift_source_digest(source: Path, records: list[tuple[str, str, str]]) -> str:
    prefix = PROVISIONER_SOURCE_ROOT + "/"
    expected = [
        (path, blob)
        for path, _, blob in records
        if path.startswith(prefix) and path.endswith(".swift")
    ]
    if (
        not expected
        or expected != sorted(expected)
        or tuple(path for path, _ in expected) != CANONICAL_PROVISIONER_SWIFT_SOURCE_PATHS
        or len({path.casefold() for path, _ in expected}) != len(expected)
    ):
        raise ValueError("provisioner Swift source tree is not a closed canonical set")
    rows: list[dict[str, str]] = []
    observed: set[str] = set()
    for path, blob in expected:
        file = source.joinpath(*path.split("/"))
        payload = _private_bytes(file, MAX_SWIFT_SOURCE_BYTES, "reviewed Swift source")
        git_blob = hashlib.sha1(
            b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
        ).hexdigest()
        if git_blob != blob:
            raise ValueError("live provisioner Swift source differs from Git evidence")
        observed.add(path)
        rows.append({"path": path, "blob": blob, "sha256": _sha(payload)})
    live = {
        path.relative_to(source).as_posix()
        for path in (source / PROVISIONER_SOURCE_ROOT).rglob("*.swift")
    }
    if live != observed:
        raise ValueError(
            "provisioner Swift source has extra, missing, or nontracked files"
        )
    return _domain_sha(PROVISIONER_SWIFT_SOURCE_DOMAIN, rows)


def _require_directory_entry(
    parent: int, name: str, label: str
) -> tuple[int, tuple[int, int]]:
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(named.st_mode) or stat.S_IMODE(named.st_mode) != 0o555:
        raise ValueError(f"bundle {label} is not a directory")
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o555
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(descriptor)
        raise ValueError(f"bundle {label} rebound during open")
    return descriptor, (opened.st_dev, opened.st_ino)


def _read_held_regular(
    parent: int, name: str, label: str, maximum_bytes: int
) -> tuple[bytes, os.stat_result]:
    _require_bounded_nonnegative_int(
        maximum_bytes, f"bundle {label} maximum size", MAX_TRANSPORT_BYTES
    )
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        payload = _read_private_descriptor(descriptor, f"bundle {label}", maximum_bytes)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size)
        if (
            len(payload) != opened.st_size
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat.S_ISREG(rebound.st_mode)
            or rebound.st_nlink != 1
            or any(
                (item.st_dev, item.st_ino, item.st_nlink, item.st_size) != identity
                for item in (after, rebound)
            )
        ):
            raise ValueError(f"bundle {label} changed during held read")
        return payload, after
    finally:
        os.close(descriptor)


class _HeldTreeBudget:
    """Checked row/depth/byte accounting shared by one held tree traversal."""

    def __init__(self, *, max_rows: int, max_depth: int, max_bytes: int) -> None:
        self.max_rows = _require_bounded_nonnegative_int(
            max_rows, "held transport row limit", MAX_TRANSPORT_ROWS
        )
        self.max_depth = _require_bounded_nonnegative_int(
            max_depth, "held transport path-depth limit", MAX_TRANSPORT_PATH_DEPTH
        )
        self.max_bytes = _require_bounded_nonnegative_int(
            max_bytes, "held transport byte limit", MAX_TRANSPORT_BYTES
        )
        self.rows = 0
        self.declared_bytes = 0
        self.observed_bytes = 0

    def add_row(self, path: str, size: int) -> None:
        _require_bounded_nonnegative_int(size, "held transport row size", self.max_bytes)
        depth = len(path.split("/")) - 1
        if depth > self.max_depth or self.rows >= self.max_rows:
            raise ValueError("held transport exceeds row or path-depth contract")
        self.rows += 1

    def _add(self, value: int, *, observed: bool) -> None:
        value = _require_bounded_nonnegative_int(
            value, "held transport size", self.max_bytes
        )
        current = self.observed_bytes if observed else self.declared_bytes
        if value > self.max_bytes - current:
            raise ValueError("held transport exceeds aggregate byte contract")
        if observed:
            self.observed_bytes += value
        else:
            self.declared_bytes += value

    def reserve(self, value: int) -> None:
        self._add(value, observed=False)

    def observe(self, value: int) -> None:
        self._add(value, observed=True)


def _hash_held_regular(
    parent: int,
    name: str,
    label: str,
    maximum_bytes: int,
    budget: _HeldTreeBudget,
    expected_size: int,
) -> tuple[str, os.stat_result]:
    """Hash a held private regular file without retaining its bytes in memory."""
    maximum_bytes = _require_bounded_nonnegative_int(
        maximum_bytes, f"bundle {label} maximum size", MAX_TRANSPORT_BYTES
    )
    expected_size = _require_bounded_nonnegative_int(
        expected_size, f"bundle {label} declared size", maximum_bytes
    )
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size < 0
            or opened.st_size > maximum_bytes
            or opened.st_size != expected_size
        ):
            raise ValueError(f"bundle {label} is not the declared bounded private regular file")
        budget.observe(opened.st_size)
        remaining = opened.st_size
        digest = hashlib.sha256()
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise ValueError(f"bundle {label} changed during held hash")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError(f"bundle {label} exceeded its declared size")
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size)
        if any(
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or (item.st_dev, item.st_ino, item.st_nlink, item.st_size) != identity
            for item in (after, rebound)
        ):
            raise ValueError(f"bundle {label} changed during held hash")
        return digest.hexdigest(), after
    finally:
        os.close(descriptor)


def _require_unique_checkout_names(names: Sequence[str]) -> list[str]:
    if (
        any(not isinstance(name, str) or not name or name in {".", ".."} for name in names)
        or len(set(names)) != len(names)
        or len({name.casefold() for name in names}) != len(names)
    ):
        raise ValueError("bundle checkout has exact or ASCII-casefold duplicate names")
    return sorted(names)


def _held_checkout_entries(
    descriptor: int,
    expected: dict[str, tuple[str, int]],
    label: str = "checkout",
    budget: _HeldTreeBudget | None = None,
) -> list[dict[str, Any]]:
    if budget is None:
        budget = _HeldTreeBudget(
            max_rows=MAX_TRANSPORT_ROWS,
            max_depth=MAX_TRANSPORT_PATH_DEPTH,
            max_bytes=MAX_CHECKOUT_TOTAL_BYTES,
        )
    if expected.get(label) != ("directory", 0):
        raise ValueError("bundle checkout manifest directory is malformed")
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o555:
        raise ValueError("bundle checkout is not a directory")
    names = _require_unique_checkout_names(os.listdir(descriptor))
    prefix = label + "/"
    expected_children = {
        path[len(prefix) :]: value
        for path, value in expected.items()
        if path.startswith(prefix) and "/" not in path[len(prefix) :]
    }
    if set(names) != set(expected_children):
        raise ValueError("bundle checkout children differ from manifest")
    rows: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    for name in names:
        child_label = f"{label}/{name}"
        expected_type, expected_size = expected_children[name]
        budget.add_row(child_label, 0)
        if expected_type == "directory":
            if expected_size != 0:
                raise ValueError("bundle checkout manifest directory is malformed")
            child, identity = _require_directory_entry(descriptor, name, child_label)
            try:
                child_rows = _held_checkout_entries(child, expected, child_label, budget)
            finally:
                os.close(child)
            rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (rebound.st_dev, rebound.st_ino) != identity:
                raise ValueError("bundle checkout directory rebound")
            row = {
                "path": child_label,
                "type": "directory",
                "mode": "0555",
                "size": 0,
                "sha256": _domain_sha(
                    "R4DIR\0",
                    [
                        {
                            "name": entry["path"].rsplit("/", 1)[-1],
                            "type": entry["type"],
                            "mode": entry["mode"],
                            "size": entry["size"],
                            "sha256": entry["sha256"],
                        }
                        for entry in child_rows
                        if entry["path"].rsplit("/", 1)[0] == child_label
                    ],
                ),
            }
            rows.extend(child_rows)
        elif expected_type == "regular":
            budget.reserve(expected_size)
            digest, stable = _hash_held_regular(
                descriptor,
                name,
                child_label,
                MAX_CHECKOUT_ENTRY_BYTES,
                budget,
                expected_size,
            )
            row = {
                "path": child_label,
                "type": "regular",
                "mode": f"{stat.S_IMODE(stable.st_mode):04o}",
                "size": stable.st_size,
                "sha256": digest,
            }
        else:
            raise ValueError("bundle checkout manifest entry is malformed")
        if expected_type != "directory":
            rows.append(row)
        children.append(
            {
                "name": name,
                "type": row["type"],
                "mode": row["mode"],
                "size": row["size"],
                "sha256": row["sha256"],
            }
        )
    after = os.fstat(descriptor)
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino) or sorted(
        os.listdir(descriptor)
    ) != names or _require_unique_checkout_names(os.listdir(descriptor)) != names:
        raise ValueError("bundle checkout changed during held traversal")
    rows.append(
        {
            "path": label,
            "type": "directory",
            "mode": "0555",
            "size": 0,
            "sha256": _domain_sha("R4DIR\0", children),
        }
    )
    return sorted(rows, key=lambda row: row["path"])


def _validate_manifest_checkout_budget(entries: list[object]) -> None:
    """Bound closed manifest checkout rows before walking their live counterparts."""
    budget = _HeldTreeBudget(
        max_rows=MAX_TRANSPORT_ROWS,
        max_depth=MAX_TRANSPORT_PATH_DEPTH,
        max_bytes=MAX_CHECKOUT_TOTAL_BYTES,
    )
    for row in entries:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("type"), str)
            or type(row.get("size")) is not int
            or row["size"] < 0
            or row["size"] > MAX_CHECKOUT_ENTRY_BYTES
        ):
            raise ValueError("bundle checkout manifest row is malformed")
        budget.add_row(row["path"], row["size"])
        if row["type"] == "regular":
            budget.reserve(row["size"])
        elif row["type"] != "directory" or row["size"] != 0:
            raise ValueError("bundle checkout manifest row is malformed")


def _expected_checkout_entry_map(
    entries: list[object],
) -> dict[str, tuple[str, int]]:
    """Close manifest checkout type/size declarations before live descriptor traversal."""
    _validate_manifest_checkout_budget(entries)
    expected: dict[str, tuple[str, int]] = {}
    prior_path = ""
    required_keys = {"path", "type", "mode", "size", "sha256"}
    for row in entries:
        if not isinstance(row, dict) or set(row) != required_keys:
            raise ValueError("bundle checkout manifest row is malformed")
        path = row["path"]
        type_ = row["type"]
        size = row["size"]
        mode = row["mode"]
        digest = row["sha256"]
        parts = path.split("/")
        if (
            not isinstance(path, str)
            or path <= prior_path
            or path in expected
            or not parts
            or parts[0] != "checkout"
            or any(part in {"", ".", ".."} for part in parts)
            or not isinstance(type_, str)
            or type(size) is not int
            or size < 0
            or size > MAX_CHECKOUT_ENTRY_BYTES
            or not isinstance(mode, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("bundle checkout manifest row is malformed")
        if type_ == "directory":
            if size != 0 or mode != "0555":
                raise ValueError("bundle checkout manifest directory is malformed")
        elif type_ != "regular" or mode not in {"0444", "0555"}:
            raise ValueError("bundle checkout manifest row is malformed")
        expected[path] = (type_, size)
        prior_path = path
    if expected.get("checkout") != ("directory", 0):
        raise ValueError("bundle checkout manifest root is malformed")
    for path in expected:
        if path != "checkout" and expected.get(path.rsplit("/", 1)[0]) != (
            "directory",
            0,
        ):
            raise ValueError("bundle checkout manifest parents are malformed")
    return expected


def _held_bundle_evidence(bundle: Path) -> tuple[dict[str, Any], bytes, bytes, list[dict[str, Any]]]:
    if not bundle.is_absolute():
        raise ValueError("bundle must be an absolute directory")
    if bundle.is_symlink():
        raise ValueError("bundle must not be a symlink")
    with open_trusted_root(bundle.resolve(strict=True)) as root:
        root.assert_bound()
        expected_names = {
            "R4_RUNTIME_INPUT_BUNDLE_V2.json",
            "r4_protected_python311.lock.json",
            "materialization-policy.json",
            "artifacts",
            "checkout",
        }
        if set(os.listdir(root.root_fd)) != expected_names:
            raise ValueError("bundle layout is not closed")
        manifest_bytes, _ = _read_held_regular(
            root.root_fd,
            "R4_RUNTIME_INPUT_BUNDLE_V2.json",
            "manifest",
            MAX_BUNDLE_MANIFEST_BYTES,
        )
        manifest = _manifest_bytes(manifest_bytes)
        lock, _ = _read_held_regular(
            root.root_fd, "r4_protected_python311.lock.json", "lock", MAX_LOCK_BYTES
        )
        policy, _ = _read_held_regular(
            root.root_fd,
            "materialization-policy.json",
            "policy",
            MAX_POLICY_BYTES,
        )
        artifacts_fd, artifacts_identity = _require_directory_entry(
            root.root_fd, "artifacts", "artifacts"
        )
        checkout_fd, checkout_identity = _require_directory_entry(
            root.root_fd, "checkout", "checkout"
        )
        try:
            artifacts = manifest.get("artifacts")
            expected_checkout_entries = manifest.get("checkout_entries")
            if (
                not isinstance(artifacts, list)
                or not artifacts
                or len(artifacts) > MAX_TRANSPORT_ROWS
                or not isinstance(expected_checkout_entries, list)
                or len(expected_checkout_entries) > MAX_TRANSPORT_ROWS
            ):
                raise ValueError("bundle artifacts are malformed")
            expected_checkout = _expected_checkout_entry_map(expected_checkout_entries)
            artifact_budget = _HeldTreeBudget(
                max_rows=MAX_TRANSPORT_ROWS,
                max_depth=1,
                max_bytes=MAX_ARTIFACT_TOTAL_BYTES,
            )
            expected_artifact_names: list[str] = []
            for index, row in enumerate(artifacts):
                if not isinstance(row, dict) or set(row) != {
                    "path", "kind", "filename", "version", "url", "sha256", "size"
                }:
                    raise ValueError("bundle artifact row is malformed")
                filename = row.get("filename")
                if (
                    not isinstance(filename, str)
                    or not filename
                    or filename in {".", ".."}
                    or "/" in filename
                    or "\\" in filename
                    or type(row.get("size")) is not int
                    or row["size"] < 0
                    or row["size"] > MAX_ARTIFACT_BYTES
                    or not isinstance(row.get("sha256"), str)
                    or not isinstance(row.get("version"), str)
                    or not isinstance(row.get("url"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                    or row.get("path") != f"artifacts/{filename}"
                    or row.get("kind") != ("python_archive" if index == 0 else "wheel")
                ):
                    raise ValueError("bundle artifact row is malformed")
                artifact_budget.add_row(f"artifacts/{filename}", 0)
                artifact_budget.reserve(row["size"])
                digest, stable = _hash_held_regular(
                    artifacts_fd,
                    filename,
                    f"artifacts/{filename}",
                    MAX_ARTIFACT_BYTES,
                    artifact_budget,
                    row["size"],
                )
                if stable.st_size != row["size"] or digest != row["sha256"]:
                    raise ValueError("bundle artifact evidence differs from held bytes")
                expected_artifact_names.append(filename)
            if (
                len(expected_artifact_names) != len(set(expected_artifact_names))
                or len({name.casefold() for name in expected_artifact_names})
                != len(expected_artifact_names)
                or sorted(os.listdir(artifacts_fd)) != sorted(expected_artifact_names)
            ):
                raise ValueError("bundle artifacts are not an exact closed set")
            checkout_entries = _held_checkout_entries(checkout_fd, expected_checkout)
            if expected_checkout_entries != checkout_entries:
                raise ValueError("bundle checkout evidence differs from held bytes")
        finally:
            os.close(checkout_fd)
            os.close(artifacts_fd)
        for name, identity in (("artifacts", artifacts_identity), ("checkout", checkout_identity)):
            rebound = os.stat(name, dir_fd=root.root_fd, follow_symlinks=False)
            if (rebound.st_dev, rebound.st_ino) != identity:
                raise ValueError("bundle directory rebound")
        root.assert_bound()
        if set(os.listdir(root.root_fd)) != expected_names:
            raise ValueError("bundle layout changed during held validation")
        return manifest, lock, policy, checkout_entries


def _helper_hashes(root: Path) -> list[dict[str, str]]:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("helper build must be an absolute private directory")
    required = (
        "bootstrap/provisioner",
        "bootstrap/provisioner.codesign-requirement.txt",
    )
    observed = []
    for relative in required:
        maximum = (
            MAX_HELPER_BINARY_BYTES
            if relative.endswith("/provisioner")
            else MAX_HELPER_REQUIREMENT_BYTES
        )
        payload = _private_bytes(root / relative, maximum, f"helper {relative}")
        observed.append({"path": relative, "sha256": _sha(payload)})
    names = {
        item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()
    }
    if names != set(required):
        raise ValueError("helper build must contain exactly the two bootstrap helpers")
    return observed


def _wheel_set(artifacts: object) -> str:
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not isinstance(artifacts[0], dict)
    ):
        raise ValueError("bundle artifacts are malformed")
    wheels = []
    for row in artifacts[1:]:
        if not isinstance(row, dict) or row.get("kind") != "wheel":
            raise ValueError("bundle wheel set is malformed")
        wheels.append(
            {
                key: row.get(key)
                for key in ("filename", "version", "url", "sha256", "size")
            }
        )
    return _domain_sha("R4V2_WHEEL_SET\0", wheels)


def _review_package_output_hook(_stage: str) -> None:
    """Deterministic output-race seam; production deliberately does nothing."""


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise ValueError("review package write made no progress")
        offset += written


def _rebind_written_review_package(
    parent: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int, int],
    payload: bytes,
) -> None:
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(descriptor)
    expected = (*identity, len(payload))
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (named.st_dev, named.st_ino, named.st_nlink, named.st_size) != expected
        or (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size) != expected
    ):
        raise ValueError("review package output rebound during write")
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = b"".join(iter(lambda: os.read(descriptor, 1024 * 1024), b""))
    after = os.fstat(descriptor)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        observed != payload
        or any(
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or (item.st_dev, item.st_ino, item.st_nlink, item.st_size) != expected
            for item in (after, rebound)
        )
    ):
        raise ValueError("review package output changed during write")


def _remove_owned_review_package(
    parent: int, name: str, identity: tuple[int, int, int]
) -> None:
    try:
        candidate = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            stat.S_ISREG(candidate.st_mode)
            and (candidate.st_dev, candidate.st_ino, candidate.st_nlink) == identity
        ):
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
    except FileNotFoundError:
        return


def _write_review_package_exclusive(review_package: Path, payload: bytes) -> None:
    if not review_package.is_absolute() or review_package.name in {"", ".", ".."}:
        raise ValueError("review package output must be a fresh absolute path")
    descriptor = -1
    identity: tuple[int, int, int] | None = None
    parent_path = review_package.parent.resolve(strict=True)
    with open_trusted_root(parent_path) as trusted_parent:
        parent = trusted_parent.root_fd
        try:
            trusted_parent.assert_bound()
            descriptor = os.open(
                review_package.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o400,
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValueError("review package output is not a private regular file")
            identity = (opened.st_dev, opened.st_ino, opened.st_nlink)
            _write_all(descriptor, payload)
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            _review_package_output_hook("after_file_fsync")
            trusted_parent.assert_bound()
            _rebind_written_review_package(
                parent, review_package.name, descriptor, identity, payload
            )
            os.fsync(parent)
            _review_package_output_hook("after_parent_fsync")
            trusted_parent.assert_bound()
            _rebind_written_review_package(
                parent, review_package.name, descriptor, identity, payload
            )
        except BaseException:
            if identity is not None:
                _remove_owned_review_package(parent, review_package.name, identity)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def build_task14_review_package_v2(
    source: Path,
    bundle: Path,
    helper_build: Path,
    exact_suite_log: Path,
    review_package: Path,
    *,
    runner: GitRunner = run_recorded_git,
) -> dict[str, Any]:
    if source.is_symlink() or bundle.is_symlink():
        raise ValueError("review package inputs must not be symlinks")
    source = source.resolve(strict=True)
    held_bundle = _held_bundle_evidence(bundle)
    manifest, lock, policy, _checkout_entries = held_bundle
    head, tree, rows, records = _required_rows(source, runner)
    if manifest.get("git") != {"head": head, "tree": tree, "required_blobs": rows}:
        raise ValueError(
            "bundle Git evidence does not equal independently rebuilt evidence"
        )
    validate_required_blob_rows(rows)
    policy_digest = _v2_document_digest(
        policy, kind="R4_PRIVILEGED_MATERIALIZATION_POLICY_V2"
    )
    if manifest.get("lock_sha256") != _sha(lock) or manifest.get(
        "materialization_policy_sha256"
    ) != policy_digest:
        raise ValueError("bundle lock or policy digest mismatch")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or not isinstance(artifacts[0], dict)
        or artifacts[0].get("kind") != "python_archive"
    ):
        raise ValueError("bundle archive is malformed")
    helpers = _helper_hashes(helper_build)
    suite = _private_bytes(exact_suite_log, MAX_EXACT_SUITE_LOG_BYTES, "exact suite log")
    reviewed_key_a_pin = _reviewed_swift_key_a_pin(source, records)
    if _held_bundle_evidence(bundle) != held_bundle:
        raise ValueError("bundle evidence changed during review package construction")
    package = {
        "kind": "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2",
        "namespace": NAMESPACE,
        "authority_id": AUTHORITY_ID,
        "reviewed_head": head,
        "reviewed_tree": tree,
        "required_blob_set_sha256": _domain_sha("R4V2_REQUIRED_BLOBS\0", rows),
        "exact_suite_log_sha256": _sha(suite),
        "input_bundle_manifest_sha256": manifest.get("sha256"),
        "lock_sha256": _sha(lock),
        "python_archive_sha256": artifacts[0].get("sha256"),
        "wheel_set_sha256": _wheel_set(artifacts),
        "materialization_policy_sha256": policy_digest,
        "provisioner_swift_source_sha256": _swift_source_digest(source, records),
        "provisioner_binary_sha256": helpers[0]["sha256"],
        "provisioner_designated_requirement_sha256": helpers[1]["sha256"],
        "helper_hashes": helpers,
        "helper_team_id": HELPER_TEAM_ID,
        "helper_signing_identity_sha1": HELPER_SIGNING_IDENTITY_SHA1,
        "task14_approval_verification_public_key_sha256": reviewed_key_a_pin,
        "sha256": "",
    }
    if (
        not isinstance(package["python_archive_sha256"], str)
        or len(package["python_archive_sha256"]) != 64
    ):
        raise ValueError("bundle archive digest is malformed")
    package["sha256"] = _sha(
        _canonical({key: value for key, value in package.items() if key != "sha256"})
    )
    payload = _canonical(package) + b"\n"
    _write_review_package_exclusive(review_package, payload)
    return _validate_review_package(package, payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--helper-build", type=Path)
    parser.add_argument("--exact-suite-log", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--review-package", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.verify_only:
        if any((args.repository, args.bundle, args.helper_build, args.exact_suite_log)):
            parser.error("--verify-only reads only --review-package")
        verify_task14_review_package_v2(args.review_package)
    else:
        if not all((args.repository, args.bundle, args.helper_build, args.exact_suite_log)):
            parser.error("create requires repository, bundle, helper build, and exact suite log")
        build_task14_review_package_v2(
            args.repository,
            args.bundle,
            args.helper_build,
            args.exact_suite_log,
            args.review_package,
            runner=run_recorded_git,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
