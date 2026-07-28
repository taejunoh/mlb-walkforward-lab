"""Shared, import-safe recovery parent evidence fixtures.

This fixture exposes only the immutable parent authority surface needed while
the recovery execution helpers are introduced by later tasks.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mlb_backtest.v52_recovery_parent_evidence import (
    PARENT_RELATIVES,
    RecoveryParentCatalog,
    TreeFingerprint,
    build_recovery_parent_catalog,
    capture_tree_fingerprint,
)


_TEST_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_PARENT_TREE = (
    Path("data/v5_2_qd/historical"),
    Path("audit/v5_2_qd/historical"),
)
SUPERSEDING_PARENT_TREE = (
    Path("data/v5_2_qd/historical_superseding_565534"),
    Path("audit/v5_2_qd/historical_superseding_565534"),
)


def _parent_evidence_root() -> Path:
    """Locate the checked-out immutable evidence without copying or mutating it."""
    candidates = (
        _TEST_REPOSITORY_ROOT,
        _TEST_REPOSITORY_ROOT.parent / "mlb-v5-1-backtest",
    )
    for candidate in candidates:
        if all((candidate / relative).is_dir() for relative in ORIGINAL_PARENT_TREE):
            if all(
                (candidate / relative).is_dir() for relative in SUPERSEDING_PARENT_TREE
            ):
                return candidate
    raise FileNotFoundError("the immutable recovery parent evidence trees are absent")


ROOT = _parent_evidence_root()


def recovery_parent_catalog(root: Path = ROOT) -> RecoveryParentCatalog:
    return build_recovery_parent_catalog(root)


def parent_tree_fingerprints(root: Path = ROOT) -> tuple[TreeFingerprint, ...]:
    return tuple(
        capture_tree_fingerprint(root, relative)
        for relative in ORIGINAL_PARENT_TREE + SUPERSEDING_PARENT_TREE
    )


def populate_isolated_parent_evidence(destination: Path, source: Path = ROOT) -> None:
    """Copy only the closed parent-object directories into an isolated root."""
    directories: set[Path] = set()
    for relatives in PARENT_RELATIVES.values():
        registration = relatives["registration"]
        quarantine = relatives["quarantine"]
        directories.add(registration.parent)
        directories.add(quarantine.parent)
    for relative in sorted(directories):
        shutil.copytree(source / relative, destination / relative)


__all__ = [
    "ORIGINAL_PARENT_TREE",
    "PARENT_RELATIVES",
    "ROOT",
    "SUPERSEDING_PARENT_TREE",
    "parent_tree_fingerprints",
    "populate_isolated_parent_evidence",
    "recovery_parent_catalog",
]
