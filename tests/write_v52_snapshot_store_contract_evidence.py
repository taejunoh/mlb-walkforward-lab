"""Generate honest, hermetic evidence for the v5.2 snapshot-store contract.

This test-support script exercises inventory construction, publication, and
loading entirely inside a temporary directory using deterministic fixture
snapshots.  It intentionally does not publish to the canonical historical
source tree or advance any execution phase.
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from mlb_backtest import v52_snapshot_store as store  # noqa: E402
from mlb_backtest.v52_source_contracts import canonical_json_bytes  # noqa: E402
from tests import v52_snapshot_store_fixture as fixture  # noqa: E402


REPORT = REPOSITORY / "reports" / "v5_2_qd" / "source_snapshot_store_publish.json"
SUMMARY = REPOSITORY / "reports" / "v5_2_qd" / "source_snapshot_store_publish.md"
CANONICAL_SOURCES = REPOSITORY / "data" / "v5_2_qd" / "historical" / "sources"
BY_SEASON = {
    "2018": 12,
    "2019": 12,
    "2020": 8,
    "2021": 12,
    "2022": 12,
    "2023": 13,
    "2024": 12,
}
BY_SOURCE = {
    "baseball_savant_statcast": 46,
    "mlb_schedule_game_feed": 7,
    "mlb_stats_pitcher_bf": 7,
    "mlb_stats_pitching": 7,
    "prior_schedule_history": 7,
    "venue_team_timezone_table": 7,
}
EVIDENCE_KEYS = frozenset(
    {
        "evidence_scope",
        "canonical_historical_publish_performed",
        "run_id",
        "inventory_sha256",
        "snapshot_count",
        "source_season_count",
        "source_count",
        "inventory_status",
        "by_source",
        "by_season",
        "commands",
        "protected_duplicates_untouched",
    }
)
PROTECTED_DUPLICATES = frozenset(
    {
        "mlb_backtest/v52_query_plan 2.py",
        "reports/v5_2_qd/walkforward_500_preflight 2.md",
        "schemas/v5_2_preflight.schema 2.json",
        "tests/test_v52_query_plan 2.py",
    }
)
VALIDATION_COMMANDS = [
    "python3 -m unittest discover -s tests -p 'test*.py' -v",
    "ruff check mlb_backtest tests",
]
_GIT_ENV_KEYS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)


def _protected_status_is_expected(raw: bytes) -> bool:
    """Return true only for the exact four expected untracked duplicate entries."""

    if not isinstance(raw, bytes):
        return False
    try:
        entries = []
        for item in raw.split(b"\0"):
            if not item:
                continue
            line = item.decode("utf-8")
            if len(line) < 4 or line[2] != " ":
                return False
            entries.append((line[:2], line[3:]))
    except UnicodeDecodeError:
        return False
    expected = {("??", path) for path in PROTECTED_DUPLICATES}
    return len(entries) == len(expected) and set(entries) == expected


def _protected_duplicates_untouched() -> bool:
    """Require all protected duplicates to remain untracked and unstaged."""

    paths = tuple(sorted(PROTECTED_DUPLICATES))
    env = _sanitized_git_env()
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=REPOSITORY,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        repo_root = Path(result.stdout.strip()).resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    if repo_root != REPOSITORY.resolve():
        return False
    result = subprocess.run(
        (
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *paths,
        ),
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
    )
    return _protected_status_is_expected(result.stdout)


def _sanitized_git_env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if key not in _GIT_ENV_KEYS
    }


def _assert_canonical_sources_absent() -> None:
    if os.path.lexists(CANONICAL_SOURCES):
        raise AssertionError("canonical historical source tree must remain absent")


def _atomic_write(path: Path, contents: bytes) -> None:
    """Write one report atomically, fsyncing both file and containing directory."""

    if not isinstance(contents, bytes):
        raise TypeError("atomic report contents must be bytes")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def render_evidence() -> tuple[bytes, bytes]:
    """Build deterministic evidence bytes without writing any report files."""

    _assert_canonical_sources_absent()
    protected_duplicates_untouched = _protected_duplicates_untouched()
    snapshots = fixture.fake_81()
    inputs = fixture.store_inputs()
    try:
        with TemporaryDirectory() as directory, fixture.patched_store_replay():
            root = Path(directory).resolve()
            inventory = store.build_inventory(inputs)
            receipt = store.publish_snapshot_store(
                root, inventory, inputs, lock_owner="contract-evidence"
            )
            loaded = store.load_snapshot_store(root, inputs)
            if receipt.snapshot_count != 81:
                raise AssertionError("publication receipt snapshot count differs")
            if loaded.inventory.status != "READY_CANDIDATE":
                raise AssertionError("loaded inventory status differs")
            if len(loaded.snapshots) != 81 or len(loaded.inventory.source_seasons) != 42:
                raise AssertionError("loaded inventory cardinalities differ")
        if root.exists():
            raise AssertionError("temporary publication root was not removed")
    finally:
        _assert_canonical_sources_absent()

    counts = loaded.inventory.counts
    observed_by_source = dict(counts["by_source"])
    observed_by_season = {
        str(season): count for season, count in counts["by_season"].items()
    }
    if tuple(loaded.snapshots) != tuple(sorted(snapshots, key=store.snapshot_sort_key)):
        raise AssertionError("loaded snapshots differ from deterministic fixture")
    if observed_by_source != BY_SOURCE or observed_by_season != BY_SEASON:
        raise AssertionError("fixture cardinalities differ from closed evidence")
    if not protected_duplicates_untouched:
        raise AssertionError("protected duplicate has a tracked diff or staged change")
    payload = {
        "evidence_scope": "HERMETIC_CONTRACT_FIXTURE",
        "canonical_historical_publish_performed": False,
        "run_id": inputs.replay.run_id,
        "inventory_sha256": receipt.inventory_sha256,
        "snapshot_count": len(loaded.snapshots),
        "source_season_count": len(loaded.inventory.source_seasons),
        "source_count": counts["source_count"],
        "inventory_status": loaded.inventory.status,
        "by_source": BY_SOURCE,
        "by_season": BY_SEASON,
        "commands": VALIDATION_COMMANDS,
        "protected_duplicates_untouched": protected_duplicates_untouched,
    }
    if set(payload) != EVIDENCE_KEYS:
        raise AssertionError("closed contract evidence keys differ")
    report_bytes = canonical_json_bytes(payload) + b"\n"
    summary_bytes = (
        "# v5.2 snapshot-store contract publication evidence\n\n"
        "Scope: hermetic deterministic fixture mechanics only; canonical historical "
        "publish was NOT performed.\n\n"
        "Real publish remains blocked until actual sealed SnapshotStoreInputs exist; "
        "this evidence does not claim historical authority data exists.\n\n"
        f"Inventory SHA-256: `{receipt.inventory_sha256}`\n\n"
        "Counts: 81 snapshots, 42 source-season pairs, 6 sources.\n\n"
        "Validation commands (recorded, not claimed as passed here):\n\n"
        "- `python3 -m unittest discover -s tests -p 'test*.py' -v`\n"
        "- `ruff check mlb_backtest tests`\n"
    ).encode("utf-8")
    return report_bytes, summary_bytes


def generate_to(output_dir: Path) -> tuple[bytes, bytes]:
    """Render evidence and atomically write both reports beneath ``output_dir``."""

    output_dir = Path(output_dir)
    _assert_canonical_sources_absent()
    canonical_root = CANONICAL_SOURCES.resolve()
    resolved_output = output_dir.resolve()
    if resolved_output == canonical_root or canonical_root in resolved_output.parents:
        raise AssertionError("report output cannot be under canonical historical sources")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_bytes, summary_bytes = render_evidence()
    _atomic_write(output_dir / REPORT.name, report_bytes)
    _atomic_write(output_dir / SUMMARY.name, summary_bytes)
    _assert_canonical_sources_absent()
    return report_bytes, summary_bytes


def main() -> None:
    generate_to(REPORT.parent)


if __name__ == "__main__":
    main()
