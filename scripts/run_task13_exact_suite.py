"""Run the Task 13 unittest suite against isolated design and source trees."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file  # noqa: E402
from mlb_backtest.preflight import (  # noqa: E402
    build_preflight,
    render_preflight_markdown,
)
from mlb_backtest.protocols import load_run_spec  # noqa: E402
from mlb_backtest.verify_v52_design import V51_REQUIRED_FILES  # noqa: E402
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


def _regular_file(path: Path, label: str) -> Path:
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label} must be a single-link file: {path}")
    return path


def sha256_paths(root: Path, relatives: Iterable[str | Path]) -> dict[str, str]:
    root = Path(root).resolve(strict=True)
    result: dict[str, str] = {}
    for raw_relative in relatives:
        relative = Path(raw_relative)
        result[relative.as_posix()] = sha256_file(
            _regular_file(root / relative, "input")
        )
    return result


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
            "--local",
            "--no-hardlinks",
            str(repo),
            str(destination),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    _run_git(["checkout", "--detach", head], cwd=destination)
    checked_out = _run_git(["rev-parse", "--verify", "HEAD"], cwd=destination).stdout.strip()
    if checked_out != head:
        raise RuntimeError("local clone did not checkout the source HEAD")
    return destination


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
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(Path(design_root).resolve())
    environment["MLB_V52_SOURCE_AUTHORITY_ROOT"] = str(Path(source_root).resolve())
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
        Path("audit/v5_2_qd/historical"),
    ):
        try:
            path = _walk_namespace_path(design_root, relative, required=False)
        except ValueError as exc:
            raise ValueError("design clone historical namespace is unsafe") from exc
        if path is not None:
            raise ValueError("design clone contains v5.2 historical data/audit")


def _assert_source_evidence_namespace(source_root: Path) -> None:
    source_root = Path(source_root).resolve(strict=True)
    expected = {Path(relative) for relative in OLD_EVIDENCE_RELATIVES}
    observed: set[Path] = set()
    roots = (
        (Path("data/v5_2_qd/historical"), True),
        (Path("audit/v5_2_qd/historical"), False),
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
        raise ValueError("source evidence namespace differs from the three old files")


def _assert_fixture_namespaces(design_root: Path, source_root: Path) -> None:
    _assert_no_design_historical_data(design_root)
    _assert_source_evidence_namespace(source_root)


def _assert_unchanged(before: Mapping[str, str], after_root: Path) -> None:
    after = sha256_paths(after_root, (Path(relative) for relative in before))
    if dict(before) != after:
        raise RuntimeError("Task 13 fixture input changed during exact suite")


def _assert_expected_hashes(actual: Mapping[str, str], expected: Mapping[str, str]) -> None:
    if dict(actual) != dict(expected):
        raise ValueError("fixed Task 13 evidence hash differs")


def run_exact_suite(
    *, repo: Path, v51_input_root: Path, old_v52_evidence_root: Path, python: str
) -> int:
    repo = validate_repository_root(repo)
    v51_input_root = Path(v51_input_root).resolve(strict=True)
    old_v52_evidence_root = Path(old_v52_evidence_root).resolve(strict=True)
    tracked_hashes = sha256_paths(repo, ORIGINAL_PREFLIGHT_RELATIVES)
    _assert_expected_hashes(tracked_hashes, ORIGINAL_PREFLIGHT_SHA256)
    v51_hashes = sha256_paths(v51_input_root, V51_REQUIRED_FILES)
    old_hashes = sha256_paths(old_v52_evidence_root, OLD_EVIDENCE_RELATIVES)
    _assert_expected_hashes(old_hashes, OLD_EVIDENCE_SHA256)
    raw = _regular_file(old_v52_evidence_root / OLD_RAW_RELATIVE, "old raw evidence")
    if raw.stat().st_size != OLD_RAW_BYTE_COUNT:
        raise ValueError("old raw evidence byte count differs")

    with tempfile.TemporaryDirectory(prefix="task13-exact-") as directory:
        base = Path(directory)
        design_root = clone_repo_at_head(repo, base / "design")
        source_root = clone_repo_at_head(repo, base / "source")
        copy_files_with_hashes(v51_input_root, design_root, V51_REQUIRED_FILES)
        design_input_hashes = sha256_paths(design_root, V51_REQUIRED_FILES)
        _write_fixture_preflight(design_root)
        fixture_preflight_hashes = sha256_paths(
            design_root, ORIGINAL_PREFLIGHT_RELATIVES
        )
        copy_files_with_hashes(old_v52_evidence_root, source_root, OLD_EVIDENCE_RELATIVES)
        _assert_expected_hashes(
            sha256_paths(source_root, OLD_EVIDENCE_RELATIVES), OLD_EVIDENCE_SHA256
        )
        if (source_root / OLD_RAW_RELATIVE).stat().st_size != OLD_RAW_BYTE_COUNT:
            raise ValueError("copied old raw evidence byte count differs")
        _assert_fixture_namespaces(design_root, source_root)

        completed = subprocess.run(
            build_child_command(python),
            cwd=design_root,
            env=build_child_environment(design_root, source_root),
            check=False,
        )
        _assert_unchanged(v51_hashes, v51_input_root)
        _assert_unchanged(v51_hashes, design_root)
        _assert_unchanged(design_input_hashes, design_root)
        _assert_unchanged(fixture_preflight_hashes, design_root)
        _assert_unchanged(old_hashes, old_v52_evidence_root)
        _assert_unchanged(old_hashes, source_root)
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
    parser.add_argument("--python", default=os.environ.get("PYTHON", "python3"))
    args = parser.parse_args(argv)
    return run_exact_suite(
        repo=args.repo,
        v51_input_root=args.v51_input_root,
        old_v52_evidence_root=args.old_v52_evidence_root,
        python=args.python,
    )


if __name__ == "__main__":
    raise SystemExit(main())
