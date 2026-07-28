"""Shared, import-safe recovery parent evidence fixtures.

This fixture exposes only the immutable parent authority surface needed while
the recovery execution helpers are introduced by later tasks.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import MappingProxyType

from mlb_backtest.v52_recovery_parent_evidence import (
    PARENT_RELATIVES,
    RecoveryParentCatalog,
    TreeFingerprint,
    build_recovery_parent_catalog,
    capture_tree_fingerprint,
)


_TEST_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_AUTHORITY_ROOT_ENV = "MLB_V52_SOURCE_AUTHORITY_ROOT"
ORIGINAL_PARENT_TREE = (
    Path("data/v5_2_qd/historical"),
    Path("audit/v5_2_qd/historical"),
)
SUPERSEDING_PARENT_TREE = (
    Path("data/v5_2_qd/historical_superseding_565534"),
    Path("audit/v5_2_qd/historical_superseding_565534"),
)

GOLDEN_PARENT_TREE_FINGERPRINTS = MappingProxyType(
    {
        Path("data/v5_2_qd/historical"): MappingProxyType(
            {
                "file_count": 22,
                "path_sha256": "b1256b64be7a53250be36d80c9bc645c7f606454d6010607d0258b3fefccc6b1",
                "sha256": "7653312388f3696f8e82755b36360aef7ab5c36f1e0c6af7ad311f70aaee8c5a",
            }
        ),
        Path("audit/v5_2_qd/historical"): MappingProxyType(
            {
                "file_count": 6,
                "path_sha256": "7578245a41176d52571b87b876afba65d8b320c8bb823996574bc2106d9959bd",
                "sha256": "4e9ae2dfb6e7a11463f6492a5d888d85aafa8fabac35d010ae51f075d1495eaa",
            }
        ),
        Path("data/v5_2_qd/historical_superseding_565534"): MappingProxyType(
            {
                "file_count": 40,
                "path_sha256": "fa047ccccacb65c1ddf72d71ed04b26801b20d014f5bc978cd1f9d858be5abee",
                "sha256": "27f22c97b3ad742bf0965d94119ff425d12990e431ea03701cee5848c58c7055",
            }
        ),
        Path("audit/v5_2_qd/historical_superseding_565534"): MappingProxyType(
            {
                "file_count": 8,
                "path_sha256": "d9ce5fd41d06cb3a43a82530b2f7de151aadd15ced54cb0b90d6761e767deda6",
                "sha256": "11b6cf0602bee1d4f76229ea508272a4f2a42b2a060515dc49366ee81afc0f05",
            }
        ),
    }
)


class ParentEvidenceConfigurationError(RuntimeError):
    """The exact immutable parent authority was not configured for this runner."""


def _has_fixed_parent_evidence(root: Path) -> bool:
    return root.is_dir() and all(
        (root / relative).is_file()
        for relatives in PARENT_RELATIVES.values()
        for relative in relatives.values()
    )


def _parent_evidence_root(checkout_root: Path = _TEST_REPOSITORY_ROOT) -> Path:
    """Use local evidence or the exact-runner authority-root configuration."""
    candidate = Path(checkout_root)
    if not _has_fixed_parent_evidence(candidate):
        configured = os.environ.get(SOURCE_AUTHORITY_ROOT_ENV)
        if not configured:
            raise ParentEvidenceConfigurationError(
                "immutable recovery parent evidence is absent from this checkout; "
                f"set {SOURCE_AUTHORITY_ROOT_ENV} to the exact source authority root"
            )
        candidate = Path(configured)
    try:
        build_recovery_parent_catalog(candidate)
    except (OSError, ValueError) as exc:
        raise ParentEvidenceConfigurationError(
            f"{SOURCE_AUTHORITY_ROOT_ENV} does not contain the exact immutable "
            "recovery parent evidence"
        ) from exc
    return candidate.resolve()


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
    "GOLDEN_PARENT_TREE_FINGERPRINTS",
    "PARENT_RELATIVES",
    "ParentEvidenceConfigurationError",
    "ROOT",
    "SOURCE_AUTHORITY_ROOT_ENV",
    "SUPERSEDING_PARENT_TREE",
    "parent_tree_fingerprints",
    "populate_isolated_parent_evidence",
    "recovery_parent_catalog",
]
