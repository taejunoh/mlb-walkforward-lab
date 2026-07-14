from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlb_backtest.diagnostic_report import render_markdown
from mlb_backtest.diagnostics import (
    AUDITED_DIGEST_PATHS,
    V51Evidence,
    build_diagnostic,
    canonical_json_bytes,
    deep_freeze,
    load_v51_evidence,
    sha256_file,
)
from mlb_backtest.execution_registry import load_execution_registry
from mlb_backtest.preflight import (
    PREFLIGHT_REQUIRED_KEYS,
    build_preflight,
    preflight_schema_document,
    parse_preflight,
    protected_inventory,
    render_preflight_markdown,
)
from mlb_backtest.protocols import (
    FOLD_REQUIRED_KEYS,
    RUN_REQUIRED_KEYS,
    V52_REQUIRED_KEYS,
    CURRENT_APPROVED_DESIGN_COMMIT,
    CURRENT_APPROVED_DESIGN_SHA256,
    ProspectiveSpec,
    RunSpec,
    V52Config,
    const_schema,
    expected_historical_run,
    expected_prospective_spec,
    expected_v52_config,
    load_prospective_spec,
    load_run_spec,
    load_v52_config,
    parse_change_contract,
    prospective_schema,
)
from mlb_backtest.notebook_artifact import execute_notebook_cells, notebook_sources
from mlb_backtest.report_artifact import build_chart_map, build_report_artifact


V51_PROTECTED_BASELINE_COMMIT = "a4aa8d2"
PROTECTED_TREES = ("data/v5_1_bt", "audit/v5_1_bt")
PROTECTED_FILES = ("config/v5_1_bt.json", "data/v5_1_bt/reports/summary.json")
APPROVED_PROTECTED_ADDITIONS = {
    "audit/v5_1_bt/protected_raw_inventory.json": (
        "2ec8bf0237cc5bdb00ddb6d2236ac631"
        "bb793aed9ba79f308672a9ecb90809c6"
    )
}
HISTORICAL_NAMESPACE_ROOTS = (
    "data/v5_2_qd/historical",
    "reports/v5_2_qd/historical",
    "audit/v5_2_qd/historical",
)
PROSPECTIVE_OBSERVATION_ROOTS = (
    "data/v5_2_qd/prospective_2026",
    "reports/v5_2_qd/prospective_2026",
    "audit/v5_2_qd/prospective_2026",
    "models/v5_2_qd/prospective_2026",
)
FITTED_NAMESPACE_ROOT = "models/v5_2_qd/folds"
CURRENT_SCOPE_TREE_ROOTS = (
    "schemas",
    "protocols/v5_2_qd",
    "reports/v5_1_bt",
    "reports/v5_2_qd",
    "analysis/v5_1_bt",
    "tests/fixtures/v5_2_qd/manifests",
)
CURRENT_SCOPE_SINGLE_FILES = (
    ".gitignore",
    "config/v5_2_qd.json",
    "mlb_backtest/diagnostics.py",
    "mlb_backtest/diagnostic_report.py",
    "mlb_backtest/notebook_artifact.py",
    "mlb_backtest/report_artifact.py",
    "mlb_backtest/protocols.py",
    "mlb_backtest/preflight.py",
    "mlb_backtest/verify_v52_design.py",
    "scripts/validate_report_artifact.mjs",
    "tests/test_diagnostics.py",
    "tests/test_diagnostic_report.py",
    "tests/test_notebook_artifact.py",
    "tests/test_report_artifact.py",
    "tests/test_protocols.py",
    "tests/test_preflight.py",
    "tests/test_verify_v52_design.py",
    "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md",
    "docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md",
)
FORBIDDEN_NAMESPACE_ROOTS = (
    *HISTORICAL_NAMESPACE_ROOTS,
    *PROSPECTIVE_OBSERVATION_ROOTS,
    FITTED_NAMESPACE_ROOT,
)
FINAL_HTML_PATH = Path(
    "/Users/taejunoh/Documents/Codex/2026-07-11/"
    "files-mentioned-by-the-user-mlb/outputs/mlb_v5_1_100_pick_diagnostic.html"
)
DELIVERY_RECEIPT_RELATIVE = "reports/v5_1_bt/report_delivery_receipt.json"
EXPECTED_CURRENT_FILES = (
    "reports/v5_1_bt/diagnostic_100.json",
    "reports/v5_1_bt/diagnostic_100.md",
    "analysis/v5_1_bt/diagnostic_100.ipynb",
    "reports/v5_1_bt/diagnostic_100_artifact.json",
    "reports/v5_1_bt/diagnostic_100_chart_map.json",
    "config/v5_2_qd.json",
    "schemas/v5_2_qd.schema.json",
    "schemas/v5_2_run_spec.schema.json",
    "schemas/v5_2_prospective.schema.json",
    "schemas/v5_2_preflight.schema.json",
    "schemas/v5_2_execution_receipt.schema.json",
    "protocols/v5_2_qd/change_contract.json",
    "protocols/v5_2_qd/historical_walkforward_500.json",
    "protocols/v5_2_qd/prospective_2026.json",
    "reports/v5_2_qd/walkforward_500_preflight.json",
    "reports/v5_2_qd/walkforward_500_preflight.md",
)
CANONICAL_JSON_FILES = (
    "analysis/v5_1_bt/diagnostic_100.ipynb",
    "config/v5_2_qd.json",
    "protocols/v5_2_qd/change_contract.json",
    "protocols/v5_2_qd/historical_walkforward_500.json",
    "protocols/v5_2_qd/prospective_2026.json",
    "protocols/v5_2_qd/v51_protected_inventory.json",
    "reports/v5_1_bt/diagnostic_100.json",
    "reports/v5_1_bt/diagnostic_100_artifact.json",
    "reports/v5_1_bt/diagnostic_100_chart_map.json",
    "reports/v5_2_qd/walkforward_500_preflight.json",
    "schemas/v5_2_preflight.schema.json",
    "schemas/v5_2_execution_receipt.schema.json",
    "schemas/v5_2_prospective.schema.json",
    "schemas/v5_2_qd.schema.json",
    "schemas/v5_2_run_spec.schema.json",
)
FORBIDDEN_CLAIMS = (
    "시장 대비 우위가 입증",
    "ROI가 양수",
    "CLV가 양수",
    "수익성이 입증",
    "actionable betting",
    "predictive improvement established",
)
V51_REQUIRED_FILES = tuple(
    sorted(
        {
            *AUDITED_DIGEST_PATHS.values(),
            "audit/v5_1_bt/lock_manifest.json",
            "audit/v5_1_bt/manual_audit.md",
            "config/v5_1_bt.json",
        }
    )
)


def _lexical_root(root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(root)))
    for node in (*reversed(candidate.parents), candidate):
        try:
            file_stat = node.lstat()
        except OSError as exc:
            raise ValueError("verification root or ancestor is unavailable") from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            raise ValueError("verification root ancestor must be a no-follow directory")
    return candidate


def _stat_identity(file_stat: os.stat_result) -> dict[str, int]:
    return {
        "dev": file_stat.st_dev,
        "ino": file_stat.st_ino,
        "mode": file_stat.st_mode,
        "nlink": file_stat.st_nlink,
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "ctime_ns": file_stat.st_ctime_ns,
    }


def _regular_file_fingerprint(path: Path, label: str) -> dict[str, object]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"regular-file snapshot is unavailable: {label}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"regular-file snapshot is unsafe: {label}")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _stat_identity(before) != _stat_identity(opened)
        ):
            raise ValueError(f"regular-file snapshot changed: {label}")
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            hasher.update(chunk)
        digest = hasher.hexdigest()
    except OSError as exc:
        raise ValueError(f"regular-file snapshot is unreadable: {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ValueError(f"regular-file snapshot changed: {label}") from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or _stat_identity(before) != _stat_identity(after)
    ):
        raise ValueError(f"regular-file snapshot changed: {label}")
    return {**_stat_identity(after), "sha256": digest}


def _tree_fingerprint(
    root: Path, relative: str, *, required: bool
) -> dict[str, object]:
    root = _lexical_root(root)
    relative_path = Path(relative)
    current = root
    for part in relative_path.parts:
        current = current / part
        try:
            node_stat = current.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError(f"current-scope tree is missing: {relative}")
            return {"state": "ABSENT"}
        except OSError as exc:
            raise ValueError(f"current-scope tree is unavailable: {relative}") from exc
        if stat.S_ISLNK(node_stat.st_mode) or not stat.S_ISDIR(node_stat.st_mode):
            raise ValueError(
                f"current-scope tree contains an unsafe ancestor: {relative}"
            )

    nodes: dict[str, object] = {}

    def scan(directory: Path, key: str) -> None:
        before = directory.lstat()
        nodes[key] = {"kind": "directory", **_stat_identity(before)}
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(f"current-scope tree is unreadable: {relative}") from exc
        for entry in entries:
            child = Path(entry.path)
            child_key = child.relative_to(root).as_posix() if child != root else "."
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(
                    f"current-scope node is unavailable: {child_key}"
                ) from exc
            if entry.is_symlink():
                raise ValueError(f"current-scope tree contains symlink: {child_key}")
            if stat.S_ISDIR(child_stat.st_mode):
                scan(child, child_key)
            elif stat.S_ISREG(child_stat.st_mode):
                nodes[child_key] = {
                    "kind": "file",
                    **_regular_file_fingerprint(child, child_key),
                }
            else:
                raise ValueError(
                    f"current-scope tree contains special node: {child_key}"
                )
        after = directory.lstat()
        if _stat_identity(before) != _stat_identity(after):
            raise ValueError(f"current-scope tree changed during scan: {relative}")

    scan(current, relative)
    return {"state": "PRESENT", "nodes": nodes}


def capture_current_scope(root: Path) -> dict[str, object]:
    root = _lexical_root(root)
    return {
        "trees": {
            relative: _tree_fingerprint(root, relative, required=True)
            for relative in CURRENT_SCOPE_TREE_ROOTS
        },
        "single_files": {
            relative: _regular_file_fingerprint(
                require_regular_file(root, relative), relative
            )
            for relative in CURRENT_SCOPE_SINGLE_FILES
        },
        "forbidden_namespaces": {
            relative: _tree_fingerprint(root, relative, required=False)
            for relative in FORBIDDEN_NAMESPACE_ROOTS
        },
    }


def require_regular_file(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError("required input path escapes verification root")
    root = _lexical_root(root)
    current = root
    for part in relative_path.parts[:-1]:
        current = current / part
        try:
            file_stat = current.lstat()
        except OSError as exc:
            raise ValueError(
                f"required input ancestor is unavailable: {relative}"
            ) from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            raise ValueError(
                f"required input ancestor is a symlink or non-directory: {relative}"
            )
    path = root / relative_path
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise ValueError(f"required input is unavailable: {relative}") from exc
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise ValueError(
            f"required input must be an unaliased regular file: {relative}"
        )
    return path


def require_absolute_regular_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("required absolute input path is not absolute")
    ancestors = tuple(reversed(path.parents))
    for ancestor in ancestors:
        try:
            file_stat = ancestor.lstat()
        except OSError as exc:
            raise ValueError(
                f"required absolute input ancestor is unavailable: {path}"
            ) from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            raise ValueError(f"required absolute input ancestor is a symlink: {path}")
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise ValueError(f"required absolute input is unavailable: {path}") from exc
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise ValueError(
            f"required absolute input must be an unaliased regular file: {path}"
        )
    return path


def _absolute_regular_file_fingerprint(path: Path) -> dict[str, object]:
    require_absolute_regular_file(path)
    return _regular_file_fingerprint(path, str(path))


def load_canonical_json(root: Path, relative: str) -> object:
    path = require_regular_file(root, relative)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"required canonical JSON is unreadable: {relative}") from exc
    if raw != canonical_json_bytes(payload) + b"\n":
        raise ValueError(f"required file is not canonical JSON: {relative}")
    return payload


def validate_delivery_paths(root: Path, outputs: Path) -> tuple[Path, Path]:
    root = _lexical_root(root)
    candidate = outputs if outputs.is_absolute() else Path.cwd() / outputs
    if candidate != FINAL_HTML_PATH.parent:
        raise ValueError("delivery output path mismatch")
    return FINAL_HTML_PATH, root / DELIVERY_RECEIPT_RELATIVE


def _expected_portable_counts(artifact: Mapping[str, object]) -> dict[str, int]:
    manifest = artifact.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("delivery receipt artifact manifest is missing")
    blocks = manifest.get("blocks")
    charts = manifest.get("charts")
    tables = manifest.get("tables")
    if not all(isinstance(value, list) for value in (blocks, charts, tables)):
        raise ValueError("delivery receipt artifact layout is malformed")
    chart_ids = {row.get("id") for row in charts if isinstance(row, Mapping)}
    table_ids = {row.get("id") for row in tables if isinstance(row, Mapping)}
    counts = {"blocks": 0, "charts": 0, "html": 0, "metrics": 0, "tables": 0}
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("delivery receipt artifact block is malformed")
        block_type = block.get("type")
        if block_type == "metric-strip":
            card_ids = block.get("cardIds")
            if not isinstance(card_ids, list):
                raise ValueError("delivery receipt metric strip is malformed")
            counts["metrics"] += len(card_ids)
            counts["blocks"] += len(card_ids)
            continue
        counts["blocks"] += 1
        if block_type == "chart" and block.get("chartId") in chart_ids:
            counts["charts"] += 1
        if block_type == "table" and block.get("tableId") in table_ids:
            counts["tables"] += 1
        if block_type == "html":
            counts["html"] += 1
    return counts


def _exact_counts_match(actual: object, expected: Mapping[str, int]) -> bool:
    return (
        isinstance(actual, dict)
        and set(actual) == set(expected)
        and all(
            not isinstance(value, bool) and isinstance(value, int)
            for value in actual.values()
        )
        and actual == expected
    )


def validate_delivery_receipt_payload(
    raw: bytes, artifact: Mapping[str, object]
) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("delivery receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("delivery receipt must be an object")
    expected_key_order = (
        "ok",
        "html",
        "stages",
        "counts",
        "sourceDialog",
        "sourceInteraction",
        "viewports",
        "timings",
    )
    expected_nested_orders = {
        "stages": ("validation", "package", "verification"),
        "counts": ("blocks", "charts", "html", "metrics", "tables"),
        "timings": (
            "validateAndPackageMs",
            "staticChartExtractionMs",
            "browserRunMs",
            "readerReadyMs",
            "contentChecksMs",
            "sourceDialogMs",
            "responsiveChecksMs",
            "verificationMs",
            "totalMs",
        ),
    }
    if tuple(payload) != expected_key_order or any(
        not isinstance(payload.get(key), dict) or tuple(payload[key]) != expected_order
        for key, expected_order in expected_nested_orders.items()
    ):
        raise ValueError("delivery receipt key insertion order differs")
    try:
        installed_bytes = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("delivery receipt contains non-JSON values") from exc
    if raw != installed_bytes:
        raise ValueError(
            "delivery receipt is not exact installed JSON.stringify output"
        )
    # Installed data-analytics 0.2.8 receipts expose no artifact digest field;
    # the exact current artifact is bound separately by report:verify below.
    required = {
        "ok",
        "html",
        "stages",
        "counts",
        "sourceDialog",
        "sourceInteraction",
        "viewports",
        "timings",
    }
    if set(payload) != required or payload.get("ok") is not True:
        raise ValueError("delivery receipt keys or status differ")
    if payload.get("html") != str(FINAL_HTML_PATH):
        raise ValueError("delivery receipt HTML identity differs")
    if payload.get("stages") != {
        "validation": "passed",
        "package": "passed",
        "verification": "passed",
    }:
        raise ValueError("delivery receipt stages differ")
    counts = payload.get("counts")
    expected_counts = _expected_portable_counts(artifact)
    if not _exact_counts_match(counts, expected_counts):
        raise ValueError("delivery receipt counts differ")
    if payload.get("sourceDialog") != "passed" or payload.get(
        "sourceInteraction"
    ) not in {"keyboard_menu_semantic_click", "semantic_click"}:
        raise ValueError("delivery receipt source interaction differs")
    if payload.get("viewports") != [1440, 390]:
        raise ValueError("delivery receipt viewports differ")
    timings = payload.get("timings")
    timing_keys = {
        "validateAndPackageMs",
        "staticChartExtractionMs",
        "browserRunMs",
        "readerReadyMs",
        "contentChecksMs",
        "sourceDialogMs",
        "responsiveChecksMs",
        "verificationMs",
        "totalMs",
    }
    if not isinstance(timings, dict) or set(timings) != timing_keys:
        raise ValueError("delivery receipt timing keys differ")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in timings.values()
    ):
        raise ValueError("delivery receipt timing values differ")
    if timings["totalMs"] < timings["validateAndPackageMs"]:
        raise ValueError("delivery receipt total timing differs")
    return payload


def git(root: Path, *args: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        env=environment,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.decode(errors="replace"))
    return completed.stdout


def build_git_inventory(
    root: Path, commit: str = V51_PROTECTED_BASELINE_COMMIT
) -> dict[str, object]:
    paths = (*PROTECTED_TREES, *PROTECTED_FILES)
    raw = git(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)
    entries: dict[str, str] = {}
    for item in raw.rstrip(b"\0").split(b"\0") if raw else ():
        metadata, path = item.split(b"\t", 1)
        _mode, kind, oid = metadata.decode().split()
        if kind == "blob":
            entries[path.decode()] = oid
    return {
        "schema_version": 1,
        "approved_commit": commit,
        "algorithm": "git-blob-sha1",
        "protected_trees": list(PROTECTED_TREES),
        "protected_files": list(PROTECTED_FILES),
        "blobs": dict(sorted(entries.items())),
    }


def write_git_inventory(root: Path, commit: str) -> Path:
    path = root / "inventory.json"
    path.write_bytes(canonical_json_bytes(build_git_inventory(root, commit)) + b"\n")
    return path


def make_tiny_git_fixture(parent: Path) -> Path:
    repo = parent / "repo"
    (repo / "data/v5_1_bt/locked").mkdir(parents=True)
    (repo / "data/v5_1_bt/reports").mkdir(parents=True)
    (repo / "audit/v5_1_bt").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "data/v5_1_bt/locked/predictions.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (repo / "data/v5_1_bt/reports/summary.json").write_text("{}\n", encoding="utf-8")
    (repo / "audit/v5_1_bt/model_card.json").write_text("{}\n", encoding="utf-8")
    (repo / "config/v5_1_bt.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _decode_nul_paths(raw: bytes) -> set[str]:
    return {item.decode() for item in raw.rstrip(b"\0").split(b"\0") if item}


def verify_protected_inventory(root: Path, inventory_path: Path) -> None:
    try:
        relative_inventory = inventory_path.relative_to(root).as_posix()
        inventory_file = require_regular_file(root, relative_inventory)
        inventory_bytes = inventory_file.read_bytes()
        expected = json.loads(inventory_bytes)
        if inventory_bytes != canonical_json_bytes(expected) + b"\n":
            raise ValueError
        commit = expected["approved_commit"]
        if not isinstance(commit, str) or expected != build_git_inventory(root, commit):
            raise ValueError
        baseline = expected["blobs"]
        if not isinstance(baseline, dict):
            raise ValueError
        tracked = _decode_nul_paths(
            git(root, "ls-files", "-z", "--", *PROTECTED_TREES, *PROTECTED_FILES)
        )
        approved_additions = (
            APPROVED_PROTECTED_ADDITIONS
            if commit == V51_PROTECTED_BASELINE_COMMIT
            else {}
        )
        if tracked != set(baseline) | set(approved_additions):
            raise ValueError
        for relative, oid in baseline.items():
            require_regular_file(root, relative)
            if (
                not isinstance(relative, str)
                or not isinstance(oid, str)
                or git(root, "hash-object", "--", relative).decode().strip() != oid
            ):
                raise ValueError
        for relative, digest in approved_additions.items():
            path = require_regular_file(root, relative)
            if sha256_file(path) != digest:
                raise ValueError
        changes = (
            git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *PROTECTED_TREES,
                *PROTECTED_FILES,
            )
            .decode()
            .splitlines()
        )
        if changes:
            raise ValueError
    except (KeyError, OSError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            "protected v5.1 inventory differs by mutation, addition, or deletion"
        ) from exc


def verify_required_inputs(root: Path) -> None:
    for relative in EXPECTED_CURRENT_FILES:
        require_regular_file(root, relative)
    for relative in V51_REQUIRED_FILES:
        require_regular_file(root, relative)
    for relative in CANONICAL_JSON_FILES:
        load_canonical_json(root, relative)


def capture_v51_integrity(
    root: Path, inventory_path: Path
) -> tuple[dict[str, object], V51Evidence]:
    """Production authority for entry/exit v5.1 integrity, independent of tests."""
    try:
        verify_protected_inventory(root, inventory_path)
        evidence = load_v51_evidence(root)
        fingerprint = {
            "approved_git_inventory": load_canonical_json(
                root, inventory_path.relative_to(root).as_posix()
            ),
            "audited_file_sha256": dict(evidence.verified_file_sha256),
            "source_file_sha256": dict(evidence.source_file_sha256),
            "predictions_content_sha256": evidence.predictions_content_sha256,
            "evaluated_content_sha256": evidence.evaluated_content_sha256,
            "protected_tree": protected_inventory(root),
        }
    except (OSError, ValueError) as exc:
        raise ValueError("v5.1 integrity verification failed") from exc
    return fingerprint, evidence


def verify_protected_snapshot(root: Path, expected: Mapping[str, object]) -> None:
    """Compare the bounded preflight guard without hashing multi-GB raw trees."""
    if protected_inventory(root) != expected:
        raise ValueError("protected evidence metadata or small sealed digest differs")


def verify_diagnostic(
    root: Path, evidence: V51Evidence | None = None
) -> dict[str, object]:
    saved_path = root / "reports/v5_1_bt/diagnostic_100.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    recomputed = build_diagnostic(
        load_v51_evidence(root) if evidence is None else evidence
    )
    if (
        saved_path.read_bytes() != canonical_json_bytes(saved) + b"\n"
        or canonical_json_bytes(saved) != canonical_json_bytes(recomputed)
        or saved["primary"]["record"] != {"W": 67, "L": 33, "N": 100}
    ):
        raise ValueError("diagnostic does not match sealed inputs")
    verify_leakage_evidence(saved)
    return saved


def verify_leakage_evidence(diagnostic: Mapping[str, object]) -> None:
    leakage = diagnostic.get("leakage")
    if not isinstance(leakage, Mapping):
        raise ValueError("diagnostic leakage evidence missing")
    statistical = leakage.get("statistical_as_of_safety")
    participant = leakage.get("participant_time_limitation")
    revision = leakage.get("source_revision_limitation")
    if not all(
        isinstance(value, Mapping) for value in (statistical, participant, revision)
    ):
        raise ValueError("diagnostic leakage evidence malformed")
    assert isinstance(statistical, Mapping)
    assert isinstance(participant, Mapping)
    assert isinstance(revision, Mapping)
    expected = {
        "status": "LIMITATION",
        "d1_feature_isolation_status": "PASS",
        "locked_row_outcome_field_isolation_status": "PASS",
        "lock_to_outcome_join_sequence_status": "NOT_TIMESTAMP_PROVEN",
        "calibration_status": "IN_SAMPLE_NOT_OOF",
    }
    if any(statistical.get(key) != value for key, value in expected.items()):
        raise ValueError("diagnostic leakage evidence differs")
    if (
        participant.get("status") != "LIMITATION"
        or participant.get("lineup") != "RECONSTRUCTED_FINAL_LINEUP"
        or participant.get("starter") != "RECONSTRUCTED_ACTUAL_STARTER"
        or revision.get("status") != "LIMITATION"
    ):
        raise ValueError("diagnostic leakage limitation differs")


def verify_markdown_agreement(root: Path, diagnostic: Mapping[str, object]) -> None:
    path = root / "reports/v5_1_bt/diagnostic_100.md"
    normalized = json.loads(canonical_json_bytes(diagnostic))
    top_six = normalized["team_concentration"]["top_six"]
    normalized["team_concentration"]["top_six"] = dict(
        sorted(top_six.items(), key=lambda item: (-item[1], item[0]))
    )
    if path.read_text(encoding="utf-8") != render_markdown(normalized):
        raise ValueError("Markdown differs from canonical diagnostic")


def verify_notebook_agreement(
    root: Path,
    diagnostic: Mapping[str, object],
    *,
    notebook_path: Path | None = None,
) -> None:
    saved_diagnostic = root / "reports/v5_1_bt/diagnostic_100.json"
    if saved_diagnostic.read_bytes() != canonical_json_bytes(diagnostic) + b"\n":
        raise ValueError("notebook diagnostic input differs")
    cells = execute_notebook_cells(
        notebook_sources(root),
        {
            "__name__": "__diagnostic_notebook__",
            "NOTEBOOK_REPOSITORY_ROOT": root.resolve(),
        },
    )
    expected = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path = (
        root / "analysis/v5_1_bt/diagnostic_100.ipynb"
        if notebook_path is None
        else notebook_path
    )
    if path.read_bytes() != canonical_json_bytes(expected) + b"\n":
        raise ValueError("notebook does not agree with deterministic execution")


def verify_artifact_agreement(
    root: Path, diagnostic: Mapping[str, object]
) -> Mapping[str, object]:
    artifact_path = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
    chart_map_path = root / "reports/v5_1_bt/diagnostic_100_chart_map.json"
    saved = json.loads(artifact_path.read_text(encoding="utf-8"))
    expected = build_report_artifact(diagnostic)
    if (
        saved != expected
        or artifact_path.read_bytes() != canonical_json_bytes(saved) + b"\n"
    ):
        raise ValueError("artifact differs from diagnostic")
    chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    if (
        chart_map != build_chart_map()
        or chart_map_path.read_bytes() != canonical_json_bytes(chart_map) + b"\n"
    ):
        raise ValueError("chart map differs from deterministic renderer")
    return saved


def verify_contract_schemas(
    root: Path,
    config: V52Config,
    historical: RunSpec,
    prospective: ProspectiveSpec,
    preflight: Mapping[str, object],
) -> None:
    v52_raw = expected_v52_config()
    historical_raw = expected_historical_run()
    prospective_raw = expected_prospective_spec()
    historical_schema = const_schema(historical_raw, RUN_REQUIRED_KEYS)
    historical_schema["properties"]["folds"] = {
        "type": "array",
        "items": {"$ref": "#/$defs/fold"},
    }
    historical_schema["$defs"] = {
        "fold": {
            "type": "object",
            "required": sorted(FOLD_REQUIRED_KEYS),
            "properties": {key: {} for key in sorted(FOLD_REQUIRED_KEYS)},
            "additionalProperties": False,
        }
    }
    documents = {
        "schemas/v5_2_qd.schema.json": const_schema(v52_raw, V52_REQUIRED_KEYS),
        "schemas/v5_2_run_spec.schema.json": historical_schema,
        "schemas/v5_2_prospective.schema.json": prospective_schema(prospective_raw),
        "schemas/v5_2_preflight.schema.json": preflight_schema_document(),
    }
    if (
        config.raw != deep_freeze(v52_raw)
        or historical.raw != deep_freeze(historical_raw)
        or prospective.raw != deep_freeze(prospective_raw)
        or set(preflight) != PREFLIGHT_REQUIRED_KEYS
    ):
        raise ValueError("schema/parser disagreement: parsed contract")
    for relative, expected in documents.items():
        if (root / relative).read_bytes() != canonical_json_bytes(expected) + b"\n":
            raise ValueError(f"schema/parser disagreement: {relative}")


def verify_change_contract(root: Path) -> Mapping[str, object]:
    contract = parse_change_contract(
        json.loads(
            (root / "protocols/v5_2_qd/change_contract.json").read_text(
                encoding="utf-8"
            )
        )
    )
    if contract["config_sha256"] != sha256_file(root / "config/v5_2_qd.json"):
        raise ValueError("change contract config digest differs")
    design_path = (
        "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
    )
    design_blob = (
        git(root, "rev-parse", f"{CURRENT_APPROVED_DESIGN_COMMIT}:{design_path}")
        .decode()
        .strip()
    )
    approved_design = git(root, "cat-file", "blob", design_blob)
    design_digest = hashlib.sha256(approved_design).hexdigest()
    if design_digest != CURRENT_APPROVED_DESIGN_SHA256:
        raise ValueError("current approved design digest differs")
    working_design = require_regular_file(root, design_path).read_bytes()
    if working_design != approved_design:
        raise ValueError("working design differs from current approved design blob")
    if contract["approved_design_blob_sha256"] != design_digest:
        raise ValueError("change contract design digest differs")
    return contract


def verify_prospective_pin(
    root: Path, prospective: ProspectiveSpec, contract: Mapping[str, object]
) -> None:
    freeze_pin = prospective.raw.get("freeze_pin")
    if not isinstance(freeze_pin, Mapping):
        raise ValueError("prospective freeze pin missing")
    if (
        freeze_pin.get("approved_design_commit") != CURRENT_APPROVED_DESIGN_COMMIT
        or freeze_pin.get("approved_design_blob_sha256")
        != contract["approved_design_blob_sha256"]
    ):
        raise ValueError("prospective design digest differs")


def verify_forbidden_claims(root: Path) -> None:
    paths = (
        root / "reports/v5_1_bt/diagnostic_100.md",
        root / "reports/v5_1_bt/diagnostic_100_artifact.json",
        root / "analysis/v5_1_bt/diagnostic_100.ipynb",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "독립 전향 검증, 시장 우위, ROI, CLV, 수익성, actionable betting 또는 "
            "v5.2 예측력 개선을 주장하지 않는다.",
            "",
        )
        if any(claim in text for claim in FORBIDDEN_CLAIMS):
            raise ValueError(f"forbidden claim in {path}")


def verify_preflight_payload(
    saved: Mapping[str, object], recomputed: Mapping[str, object]
) -> None:
    if canonical_json_bytes(saved) != canonical_json_bytes(recomputed):
        raise ValueError("saved report differs from recomputed preflight")


def verify_preflight(root: Path, historical: RunSpec) -> dict[str, object]:
    json_path = root / "reports/v5_2_qd/walkforward_500_preflight.json"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    recomputed = build_preflight(root, historical)
    verify_preflight_payload(saved, recomputed)
    payload = parse_preflight(saved)
    if json_path.read_bytes() != canonical_json_bytes(payload) + b"\n":
        raise ValueError("preflight JSON is not canonical")
    if (
        payload["status"] != "BLOCKED_REQUIRED_ACQUISITION"
        or payload["may_fit_H22"]
        or payload["fitted_model_cards_present"]
    ):
        raise ValueError("current preflight status differs")
    if payload["protected_tree_before"] != payload["protected_tree_after"]:
        raise ValueError("preflight protected evidence changed during scan")
    verify_protected_snapshot(root, payload["protected_tree_after"])
    markdown_path = root / "reports/v5_2_qd/walkforward_500_preflight.md"
    if markdown_path.read_text(encoding="utf-8") != render_preflight_markdown(payload):
        raise ValueError("preflight Markdown differs from canonical JSON")
    return payload


def fitted_model_card_count(root: Path) -> int:
    count = _namespace_regular_file_count(root, FITTED_NAMESPACE_ROOT)
    return 0 if count is None else count


def _namespace_regular_file_count(root: Path, relative: str) -> int | None:
    root = _lexical_root(root)
    relative_path = Path(relative)
    current = root
    for part in relative_path.parts:
        current = current / part
        try:
            file_stat = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"namespace node is unreadable: {relative}") from exc
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError(f"namespace contains symlink: {relative}")
        if current != root / relative_path and not stat.S_ISDIR(file_stat.st_mode):
            raise ValueError(f"namespace ancestor is an unexpected node: {relative}")
    final_stat = current.lstat()
    if not stat.S_ISDIR(final_stat.st_mode):
        raise ValueError(f"namespace root is an unexpected node: {relative}")
    stack = [current]
    count = 0
    while stack:
        directory = stack.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"namespace directory is unreadable: {relative}") from exc
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"namespace node is unreadable: {entry.path}") from exc
            if entry.is_symlink():
                raise ValueError(f"namespace contains symlink: {entry.path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_nlink != 1:
                    raise ValueError(f"namespace contains hardlink alias: {entry.path}")
                count += 1
            else:
                raise ValueError(
                    f"namespace contains special unexpected node: {entry.path}"
                )
    return count


def _namespace_tree_present(root: Path, relative: str) -> bool:
    return _namespace_regular_file_count(root, relative) is not None


def _namespace_status(root: Path, relatives: Sequence[str], absent: str) -> str:
    present = any(
        tuple(_namespace_tree_present(root, relative) for relative in relatives)
    )
    return "PARTIAL_OUTPUT_PRESENT" if present else absent


def historical_execution_status(root: Path) -> str:
    return _namespace_status(root, HISTORICAL_NAMESPACE_ROOTS, "NOT_EXECUTED")


def authenticated_historical_phase(root: Path, run_spec: RunSpec) -> str | None:
    execution = root / "audit/v5_2_qd/historical/execution"
    try:
        metadata = execution.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("execution registry namespace is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("execution registry namespace must be a no-follow directory")
    return load_execution_registry(root, run_spec).phase


def _verify_namespace_children(
    root: Path, relative: str, allowed: frozenset[str]
) -> None:
    namespace = root / relative
    try:
        metadata = namespace.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"phase namespace is unavailable: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"phase namespace is unsafe: {relative}")
    with os.scandir(namespace) as entries:
        for entry in entries:
            if entry.name not in allowed:
                raise ValueError(
                    f"forbidden phase namespace child: {relative}/{entry.name}"
                )
            _namespace_regular_file_count(root, f"{relative}/{entry.name}")


def verify_historical_phase_namespaces(root: Path, run_spec: RunSpec) -> str | None:
    phase = authenticated_historical_phase(root, run_spec)
    if phase is None:
        if historical_execution_status(root) != "NOT_EXECUTED":
            raise ValueError("historical namespace exists without an execution registry")
        if fitted_model_card_count(root):
            raise ValueError("fitted model-card namespace exists without a registry")
        return None
    if phase not in {"DESIGN_ACCEPTED", "ACQUIRING", "SOURCES_READY"}:
        raise ValueError(f"execution phase {phase} fails closed until Task 10")
    data_allowed = (
        frozenset()
        if phase == "DESIGN_ACCEPTED"
        else frozenset({"raw", "sources"})
    )
    audit_allowed = (
        frozenset({"execution"})
        if phase == "DESIGN_ACCEPTED"
        else frozenset({"acquisition", "execution"})
    )
    _verify_namespace_children(root, "data/v5_2_qd/historical", data_allowed)
    _verify_namespace_children(root, "audit/v5_2_qd/historical", audit_allowed)
    _verify_namespace_children(root, "reports/v5_2_qd/historical", frozenset())
    if _namespace_tree_present(root, FITTED_NAMESPACE_ROOT):
        raise ValueError("model-card namespace is forbidden before Task 10")
    return phase


def observation_namespace_status(root: Path) -> str:
    return _namespace_status(root, PROSPECTIVE_OBSERVATION_ROOTS, "NOT_STARTED")


def ensure_expected_files(root: Path) -> None:
    missing = [
        relative
        for relative in EXPECTED_CURRENT_FILES
        if not (root / relative).is_file()
    ]
    if missing:
        raise ValueError(f"missing current-scope files: {missing}")


def verify_namespaces_differ(historical: RunSpec, prospective: ProspectiveSpec) -> None:
    historical_namespaces = set(historical.raw["namespaces"].values())
    if prospective.namespace in historical_namespaces:
        raise ValueError("historical and prospective namespaces overlap")


def verify_html_payload(
    html_path: Path, artifact_path: Path, receipt_path: Path
) -> dict[str, object]:
    root = artifact_path.parents[2]
    expected_html, expected_receipt = validate_delivery_paths(root, html_path.parent)
    expected_artifact = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
    if (
        html_path != expected_html
        or receipt_path != expected_receipt
        or artifact_path != expected_artifact
    ):
        raise ValueError("portable HTML path mismatch")
    require_absolute_regular_file(html_path)
    artifact = load_canonical_json(root, "reports/v5_1_bt/diagnostic_100_artifact.json")
    if not isinstance(artifact, Mapping):
        raise ValueError("portable HTML artifact must be an object")
    receipt_file = require_regular_file(root, DELIVERY_RECEIPT_RELATIVE)
    receipt = validate_delivery_receipt_payload(receipt_file.read_bytes(), artifact)
    if (
        receipt_file.stat(follow_symlinks=False).st_mtime_ns
        < html_path.stat(follow_symlinks=False).st_mtime_ns
    ):
        raise ValueError("delivery receipt predates delivered HTML")
    html_before = _absolute_regular_file_fingerprint(html_path)
    plugin = Path(
        "/Users/taejunoh/.codex/plugins/cache/openai-curated-remote/"
        "data-analytics/0.2.8-13ceeea1f599"
    )
    verifier_script = (
        plugin / "skills/build-report/scripts/verify_portable_artifact.mjs"
    )
    completed = subprocess.run(
        [
            "node",
            str(verifier_script),
            "--html",
            str(html_path),
            "--artifact",
            str(artifact_path),
        ],
        cwd=plugin,
        check=False,
        capture_output=True,
        text=True,
    )
    html_after = _absolute_regular_file_fingerprint(html_path)
    if html_before != html_after:
        raise ValueError(
            "portable HTML identity or snapshot changed during verification"
        )
    try:
        output_lines = [line for line in completed.stdout.splitlines() if line]
        verification = json.loads(output_lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("portable HTML verifier returned invalid JSON") from exc
    verification_keys = {
        "ok",
        "counts",
        "html",
        "sourceDialog",
        "sourceInteraction",
        "timings",
        "viewports",
    }
    verification_timings = (
        verification.get("timings", {}) if isinstance(verification, dict) else {}
    )
    expected_verification_timing_keys = {
        "browserRunMs",
        "readerReadyMs",
        "contentChecksMs",
        "sourceDialogMs",
        "responsiveChecksMs",
        "totalMs",
    }
    timing_values_valid = (
        isinstance(verification_timings, dict)
        and set(verification_timings) == expected_verification_timing_keys
        and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value >= 0
            for value in verification_timings.values()
        )
    )
    if (
        completed.returncode != 0
        or not isinstance(verification, dict)
        or set(verification) != verification_keys
        or verification.get("ok") is not True
        or verification.get("html") != str(FINAL_HTML_PATH)
        or not _exact_counts_match(
            verification.get("counts"), _expected_portable_counts(artifact)
        )
        or verification.get("sourceDialog") != "passed"
        or verification.get("sourceInteraction")
        not in {"keyboard_menu_semantic_click", "semantic_click"}
        or verification.get("viewports") != [1440, 390]
        or not timing_values_valid
        or verification_timings["totalMs"] < verification_timings["browserRunMs"]
    ):
        raise ValueError(f"portable HTML verification failed: {completed.stderr}")
    return {
        "path": str(html_path),
        "sha256": html_before["sha256"],
        "fingerprint": html_before,
        "receipt": receipt,
    }


def build_pass_receipt(
    diagnostic: Mapping[str, object],
    config: V52Config,
    prospective: ProspectiveSpec,
    preflight: Mapping[str, object],
    fitted_count: int,
    execution: str,
    observation: str,
    inventory: Mapping[str, object],
    task11: Mapping[str, str],
    html: object,
) -> dict[str, object]:
    return {
        "status": "PASS",
        "diagnostic": {"primary": diagnostic["primary"]["record"]},
        "v52": {
            "model_version": config.model_version,
            "fitted_model_card_count": fitted_count,
        },
        "preflight": {"status": preflight["status"]},
        "historical": {"execution_status": execution},
        "prospective": {
            "first_permitted_date": prospective.first_permitted_date.isoformat(),
            "observation_status": observation,
        },
        "immutability": {
            "approved_commit": inventory["approved_commit"],
            "tracked_blob_count": len(inventory["blobs"]),
            "full_tree_guard": "metadata_path_size_mtime_inode_mode",
            "small_sealed_guard": "sha256",
        },
        "task11": dict(task11),
        "html": html,
    }


def task11_artifact_status(root: Path) -> dict[str, str]:
    paths = {
        "html": FINAL_HTML_PATH,
        "delivery_receipt": root / DELIVERY_RECEIPT_RELATIVE,
    }
    result: dict[str, str] = {}
    for name, path in paths.items():
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            result[name] = "ABSENT"
            continue
        except OSError as exc:
            raise ValueError(f"Task 11 artifact is unreadable: {path}") from exc
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
        ):
            raise ValueError(
                f"Task 11 artifact is not an unaliased regular file: {path}"
            )
        result[name] = "PRESENT"
    return result


def verify_task11_phase(status: Mapping[str, str], *, delivery_requested: bool) -> None:
    expected_value = "PRESENT" if delivery_requested else "ABSENT"
    expected = {"html": expected_value, "delivery_receipt": expected_value}
    if dict(status) != expected:
        raise ValueError("Task 11 delivery artifact differs from requested phase")


def verify_design_deliverables(root: Path, outputs: Path | None) -> dict[str, object]:
    root = _lexical_root(root)
    if outputs is not None:
        validate_delivery_paths(root, outputs)
    inventory_path = root / "protocols/v5_2_qd/v51_protected_inventory.json"
    verify_required_inputs(root)
    task11 = task11_artifact_status(root)
    verify_task11_phase(task11, delivery_requested=outputs is not None)
    entry_scope = capture_current_scope(root)
    entry_integrity, entry_evidence = capture_v51_integrity(root, inventory_path)
    inventory = load_canonical_json(
        root, "protocols/v5_2_qd/v51_protected_inventory.json"
    )
    if not isinstance(inventory, Mapping):
        raise ValueError("protected v5.1 inventory must be an object")
    if inventory["approved_commit"] != V51_PROTECTED_BASELINE_COMMIT:
        raise ValueError("protected v5.1 inventory is not pinned to approved commit")
    ensure_expected_files(root)
    diagnostic = verify_diagnostic(root, entry_evidence)
    verify_markdown_agreement(root, diagnostic)
    verify_notebook_agreement(root, diagnostic)
    verify_artifact_agreement(root, diagnostic)
    config = load_v52_config(root / "config/v5_2_qd.json")
    historical = load_run_spec(
        root / "protocols/v5_2_qd/historical_walkforward_500.json"
    )
    prospective = load_prospective_spec(
        root / "protocols/v5_2_qd/prospective_2026.json"
    )
    preflight = verify_preflight(root, historical)
    verify_contract_schemas(root, config, historical, prospective, preflight)
    contract = verify_change_contract(root)
    verify_prospective_pin(root, prospective, contract)
    verify_forbidden_claims(root)
    verify_namespaces_differ(historical, prospective)
    registry_phase = verify_historical_phase_namespaces(root, historical)
    fitted_count = fitted_model_card_count(root)
    if fitted_count:
        raise ValueError("fitted fold card exists before source acquisition")
    execution = (
        historical_execution_status(root)
        if registry_phase is None
        else registry_phase
    )
    if registry_phase is None and execution != "NOT_EXECUTED":
        raise ValueError("historical outputs exist in design-only phase")
    observation = observation_namespace_status(root)
    if observation != "NOT_STARTED":
        raise ValueError(
            "prospective observations exist before collector implementation"
        )
    html = None
    if outputs is not None:
        html = verify_html_payload(
            outputs / "mlb_v5_1_100_pick_diagnostic.html",
            root / "reports/v5_1_bt/diagnostic_100_artifact.json",
            root / "reports/v5_1_bt/report_delivery_receipt.json",
        )
    exit_integrity, _exit_evidence = capture_v51_integrity(root, inventory_path)
    if canonical_json_bytes(entry_integrity) != canonical_json_bytes(exit_integrity):
        raise ValueError("v5.1 integrity fingerprint changed during verification")
    final_fitted_count = fitted_model_card_count(root)
    if final_fitted_count:
        raise ValueError("fitted fold card exists before source acquisition")
    final_registry_phase = verify_historical_phase_namespaces(root, historical)
    final_execution = (
        historical_execution_status(root)
        if final_registry_phase is None
        else final_registry_phase
    )
    if final_registry_phase is None and final_execution != "NOT_EXECUTED":
        raise ValueError("historical outputs exist in design-only phase")
    if final_registry_phase != registry_phase or final_execution != execution:
        raise ValueError("authenticated execution phase changed during verification")
    final_observation = observation_namespace_status(root)
    if final_observation != "NOT_STARTED":
        raise ValueError(
            "prospective observations exist before collector implementation"
        )
    final_task11 = task11_artifact_status(root)
    verify_task11_phase(final_task11, delivery_requested=outputs is not None)
    exit_scope = capture_current_scope(root)
    if entry_scope != exit_scope:
        raise ValueError("current-scope fingerprint changed during verification")
    if outputs is not None:
        if not isinstance(html, Mapping):
            raise ValueError("verified HTML fingerprint is missing")
        stored_fingerprint = html.get("fingerprint")
        delivery_receipt = html.get("receipt")
        final_html_fingerprint = _absolute_regular_file_fingerprint(FINAL_HTML_PATH)
        if (
            not isinstance(stored_fingerprint, Mapping)
            or dict(stored_fingerprint) != final_html_fingerprint
            or html.get("path") != str(FINAL_HTML_PATH)
            or html.get("sha256") != final_html_fingerprint["sha256"]
            or not isinstance(delivery_receipt, Mapping)
            or delivery_receipt.get("html") != str(FINAL_HTML_PATH)
        ):
            raise ValueError("verified HTML fingerprint or identity changed")
    return build_pass_receipt(
        diagnostic,
        config,
        prospective,
        preflight,
        fitted_count,
        execution,
        observation,
        inventory,
        task11,
        html,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--outputs", type=Path)
    parser.add_argument("--skip-html", action="store_true")
    args = parser.parse_args(argv)
    if not args.skip_html and args.outputs is None:
        parser.error("--outputs is required unless --skip-html is set")
    receipt = verify_design_deliverables(
        args.root, None if args.skip_html else args.outputs
    )
    print(json.dumps(receipt, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
