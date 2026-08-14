"""Closed namespace policy for the v5.2 R4 disjoint custody authority."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .v52_r4_apfs import CUSTODY_UNAVAILABLE


GENERATION = "v5.2-r4-disjoint-custody-v2"
FINAL_NAME = "v5_1_bt_r4"
CUSTODY_PARENT = Path(
    "/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2"
)
OWNER_LOCK_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.lock"
JOURNAL_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson"
TEMPORARY_PREFIX = ".v5_1_bt_r4.disjoint-v2.issuance."
TEMPORARY_PATTERN = rf"^\.{FINAL_NAME}\.disjoint-v2\.issuance\.[0-9a-f]{{32}}\.tmp$"

DISJOINT_ARTIFACT_PATHS = MappingProxyType(
    {
        "inventory": PurePosixPath(
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/protected_raw_inventory_r4_disjoint_v2.json"
        ),
        "receipt": PurePosixPath(
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/physical_custody_transition_r4_disjoint_v2.json"
        ),
        "contract": PurePosixPath(
            "protocols/v5_2_qd/"
            "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json"
        ),
        "anchor": PurePosixPath(
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/physical_custody_authority_anchor_r4_disjoint_v2.json"
        ),
        "signature": PurePosixPath(
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/physical_custody_authority_anchor_r4_disjoint_v2.ed25519"
        ),
        "journal": PurePosixPath(
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/custody_issuance_journal_r4_disjoint_v2.ndjson"
        ),
    }
)


_POLICY_TOKEN = object()


@dataclass(frozen=True, init=False)
class DisjointCustodyPolicy:
    generation: str
    custody_parent: Path
    final_name: str
    owner_lock_name: str
    journal_name: str
    temporary_prefix: str
    temporary_pattern: str
    artifact_paths: Mapping[str, PurePosixPath]
    _token: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise CUSTODY_UNAVAILABLE("disjoint custody policy construction is unavailable")

    @classmethod
    def _from_verified(cls, **values: object) -> "DisjointCustodyPolicy":
        if values.pop("_token", None) is not _POLICY_TOKEN:
            raise CUSTODY_UNAVAILABLE("disjoint custody policy capability is unavailable")
        paths = values.get("artifact_paths")
        if not isinstance(paths, Mapping):
            raise CUSTODY_UNAVAILABLE("disjoint artifact paths are unavailable")
        obj = object.__new__(cls)
        for name in ("generation", "custody_parent", "final_name", "owner_lock_name", "journal_name", "temporary_prefix", "temporary_pattern"):
            if name not in values:
                raise CUSTODY_UNAVAILABLE("disjoint custody policy is incomplete")
            object.__setattr__(obj, name, values[name])
        object.__setattr__(obj, "artifact_paths", MappingProxyType(dict(paths)))
        object.__setattr__(obj, "_token", _POLICY_TOKEN)
        return obj

    def is_temporary_name(self, value: str) -> bool:
        return type(value) is str and re.fullmatch(self.temporary_pattern, value) is not None


PRODUCTION_POLICY = DisjointCustodyPolicy._from_verified(
    generation=GENERATION,
    custody_parent=CUSTODY_PARENT,
    final_name=FINAL_NAME,
    owner_lock_name=OWNER_LOCK_NAME,
    journal_name=JOURNAL_NAME,
    temporary_prefix=TEMPORARY_PREFIX,
    temporary_pattern=TEMPORARY_PATTERN,
    artifact_paths=DISJOINT_ARTIFACT_PATHS,
    _token=_POLICY_TOKEN,
)


def require_disjoint_policy(
    *, generation: str, custody_parent: Path, final_name: str
) -> DisjointCustodyPolicy:
    """Return the sole production policy after exact typed validation.

    Callers cannot select a parent, final leaf, or generation.  The ``Path``
    type is intentionally required here; raw command-line values must pass
    :func:`require_disjoint_cli_policy` first so spelling is validated before
    any normalization performed by ``Path``.
    """

    if (
        type(generation) is not str
        or not isinstance(custody_parent, Path)
        or type(final_name) is not str
        or generation != GENERATION
        or custody_parent != CUSTODY_PARENT
        or final_name != FINAL_NAME
    ):
        raise CUSTODY_UNAVAILABLE("disjoint custody policy differs")
    return PRODUCTION_POLICY


def require_disjoint_cli_policy(
    *, raw_generation: str, raw_parent: str, raw_final_name: str
) -> DisjointCustodyPolicy:
    """Validate un-normalized CLI literals before constructing ``Path``."""

    if (
        type(raw_generation) is not str
        or type(raw_parent) is not str
        or type(raw_final_name) is not str
        or raw_generation != GENERATION
        or raw_parent != str(CUSTODY_PARENT)
        or raw_final_name != FINAL_NAME
    ):
        raise CUSTODY_UNAVAILABLE("disjoint custody CLI policy differs")
    return require_disjoint_policy(
        generation=raw_generation,
        custody_parent=Path(raw_parent),
        final_name=raw_final_name,
    )


def is_disjoint_temporary_name(value: str) -> bool:
    """Return whether ``value`` is one canonical temporary-root spelling."""

    return PRODUCTION_POLICY.is_temporary_name(value)


__all__ = [
    "CUSTODY_PARENT",
    "DISJOINT_ARTIFACT_PATHS",
    "DisjointCustodyPolicy",
    "FINAL_NAME",
    "GENERATION",
    "JOURNAL_NAME",
    "OWNER_LOCK_NAME",
    "PRODUCTION_POLICY",
    "TEMPORARY_PATTERN",
    "TEMPORARY_PREFIX",
    "is_disjoint_temporary_name",
    "require_disjoint_cli_policy",
    "require_disjoint_policy",
]
