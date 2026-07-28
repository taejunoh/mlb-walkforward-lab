"""Shared, import-safe recovery parent evidence fixtures.

This fixture exposes only the immutable parent authority surface needed while
the recovery execution helpers are introduced by later tasks.
"""

from __future__ import annotations

import os
import shutil
import hashlib
import json
import subprocess
import tempfile
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
_SAFE_GIT_PATHS = (
    Path("/usr/bin/git"),
    Path("/opt/homebrew/bin/git"),
    Path("/usr/local/bin/git"),
)
_GIT_EXECUTABLE = next(
    (path for path in _SAFE_GIT_PATHS if path.is_file() and os.access(path, os.X_OK)),
    None,
)
if _GIT_EXECUTABLE is None:  # pragma: no cover - supported runners have Git.
    raise RuntimeError("a system Git executable is required for recovery fixtures")
_SAFE_PATH = os.pathsep.join(
    (str(_GIT_EXECUTABLE.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin")
)
SOURCE_AUTHORITY_ROOT_ENV = "MLB_V52_SOURCE_AUTHORITY_ROOT"
RECOVERY_PARENT_EVIDENCE_ROOT_ENV = "MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT"
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


def _git_run(
    arguments: list[str],
    *,
    cwd: Path,
    home: Path,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run Git with no inherited configuration, hooks, templates, or filters."""
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "HOME": str(home),
            "PATH": _SAFE_PATH,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        [
            str(_GIT_EXECUTABLE),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            *arguments,
        ],
        cwd=cwd,
        check=True,
        env=environment,
        capture_output=capture_output,
        text=text,
    )


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
        configured = os.environ.get(RECOVERY_PARENT_EVIDENCE_ROOT_ENV) or os.environ.get(
            SOURCE_AUTHORITY_ROOT_ENV
        )
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


_MATERIALIZED_SOURCE_AUTHORITIES: list[tempfile.TemporaryDirectory[str]] = []


def _has_original_2019_evidence(root: Path) -> bool:
    return root.is_dir() and all(
        (root / relative).is_file()
        for relative in PARENT_RELATIVES["original_2019_565534"].values()
    )


def _git_head(root: Path, *, home: Path) -> str | None:
    try:
        completed = _git_run(
            ["rev-parse", "HEAD"],
            cwd=root,
            home=home,
            text=True,
            capture_output=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _is_matching_source_authority(checkout_root: Path, candidate: Path) -> bool:
    if not _has_original_2019_evidence(candidate):
        return False
    with tempfile.TemporaryDirectory(
        prefix="v52-git-head-", dir=checkout_root.parent
    ) as directory:
        home = Path(directory) / "home"
        if _git_head(checkout_root, home=home) != _git_head(candidate, home=home):
            return False
    try:
        from mlb_backtest.v52_query_plan import _load_static_contracts

        _load_static_contracts(
            candidate
            / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            candidate
            / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
    except (OSError, ValueError):
        return False
    return True


def _copy_original_2019_evidence(destination: Path, source: Path) -> None:
    directories = {
        relative.parent
        for relative in PARENT_RELATIVES["original_2019_565534"].values()
    }
    for relative in sorted(directories):
        shutil.copytree(source / relative, destination / relative)


def matching_source_authority_root(
    checkout_root: Path = _TEST_REPOSITORY_ROOT,
    evidence_root: Path | None = None,
) -> Path:
    """Return a current-HEAD source authority without changing old evidence.

    An exact current authority is used directly.  A configured old authority is
    evidence-only: this helper clones the current committed checkout and copies
    only the authenticated 2019 parent files needed by the old 565534 tests.
    """
    checkout = Path(checkout_root).resolve()
    configured = evidence_root
    if configured is None:
        raw = os.environ.get(SOURCE_AUTHORITY_ROOT_ENV)
        configured = Path(raw) if raw else checkout
    evidence = Path(configured).resolve()
    if not _has_original_2019_evidence(evidence):
        raise ParentEvidenceConfigurationError(
            "immutable 2019 source evidence is absent; set "
            f"{SOURCE_AUTHORITY_ROOT_ENV} to an exact evidence root"
        )
    if _is_matching_source_authority(checkout, evidence):
        return evidence
    temporary = tempfile.TemporaryDirectory(
        prefix="v52-source-authority-", dir=checkout.parent
    )
    repository = Path(temporary.name) / "repo"
    git_home = Path(temporary.name) / "home"
    try:
        _git_run(
            ["clone", "--no-local", "-q", str(checkout), str(repository)],
            cwd=Path(temporary.name),
            home=git_home,
        )
        if _git_head(repository, home=git_home) != _git_head(checkout, home=git_home):
            raise ParentEvidenceConfigurationError(
                "materialized source authority HEAD differs from the supplied checkout"
            )
        _copy_original_2019_evidence(repository, evidence)
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )

        build_duplicate_disposition_authority(
            repository,
            source_run_spec_path=repository
            / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=repository
            / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
    except Exception:
        temporary.cleanup()
        raise
    _MATERIALIZED_SOURCE_AUTHORITIES.append(temporary)
    return repository


def recovery_authority_fixture(
    destination: Path, *, parent_evidence_root: Path = ROOT
) -> tuple[Path, Path, Path]:
    """Create the committed, rawless recovery authority used by catalog tests."""
    from mlb_backtest import protocols
    import mlb_backtest.v52_query_plan as query_plan

    repository = (destination / "repo").resolve()
    git_home = repository.parent / ".git-home"
    root = _TEST_REPOSITORY_ROOT
    design = Path(
        "docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-run-design.md"
    )
    plan = Path(
        "docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-run.md"
    )
    run_relative = Path(
        "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json"
    )
    contract_relative = Path(
        "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json"
    )
    source_contract = root / "protocols/v5_2_qd/historical_source_acquisition.json"
    for relative in (
        design,
        plan,
        Path("schemas/v5_2_acquisition_contract.schema.json"),
    ):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    run_path = repository / run_relative
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_bytes(
        _canonical_json_bytes(protocols.expected_recovery_source_run())
    )
    populate_isolated_parent_evidence(repository, parent_evidence_root)
    _git_run(["init", "-q"], cwd=repository, home=git_home)
    _git_run(
        ["config", "--local", "user.email", "recovery@example.test"],
        cwd=repository,
        home=git_home,
    )
    _git_run(
        ["config", "--local", "user.name", "Recovery Test"],
        cwd=repository,
        home=git_home,
    )
    _git_run(["add", "."], cwd=repository, home=git_home)
    _git_run(
        ["commit", "-qm", "recovery authority fixture"],
        cwd=repository,
        home=git_home,
    )

    contract = json.loads(source_contract.read_bytes())
    schedule = next(
        source
        for source in contract["sources"]
        if source["name"] == "mlb_schedule_game_feed"
    )
    schedule["parser_version"] = "v5.2.2"
    schedule["endpoints"] = [
        "https://statsapi.mlb.com/api/v1/schedule",
        "https://statsapi.mlb.com/api/v1/game/{gamePk}/boxscore",
        "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
        "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
        "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
    ]
    contract.update(
        {
            "run_id": "historical_walkforward_500_recovery_schedule_conflicts",
            "recovery_provenance": query_plan._RECOVERY_PROVENANCE,
            "canonical_roots": {
                "raw": "data/v5_2_qd/historical_recovery_schedule_conflicts/raw",
                "sources": "data/v5_2_qd/historical_recovery_schedule_conflicts/sources",
                "acquisition_audit": "audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition",
                "execution_audit": "audit/v5_2_qd/historical_recovery_schedule_conflicts/execution",
            },
            "bindings": {
                "approved_design": _binding(repository, design, "sha256", git_home),
                "implementation_plan": _binding(
                    repository, plan, "working_tree_sha256", git_home
                ),
                "run_spec": _binding(repository, run_relative, "sha256", git_home),
            },
        }
    )
    contract.pop("supersedes", None)
    contract_path = repository / contract_relative
    contract_path.write_bytes(_canonical_json_bytes(contract))
    return repository, run_path, contract_path


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _binding(
    repository: Path, relative: Path, digest_key: str, git_home: Path
) -> dict[str, str]:
    contents = (repository / relative).read_bytes()
    blob = _git_run(
        ["rev-parse", f"HEAD:{relative.as_posix()}"],
        cwd=repository,
        home=git_home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    return {
        "path": relative.as_posix(),
        digest_key: hashlib.sha256(contents).hexdigest(),
        "git_blob_id": blob,
    }


__all__ = [
    "ORIGINAL_PARENT_TREE",
    "GOLDEN_PARENT_TREE_FINGERPRINTS",
    "matching_source_authority_root",
    "PARENT_RELATIVES",
    "ParentEvidenceConfigurationError",
    "ROOT",
    "RECOVERY_PARENT_EVIDENCE_ROOT_ENV",
    "SOURCE_AUTHORITY_ROOT_ENV",
    "SUPERSEDING_PARENT_TREE",
    "parent_tree_fingerprints",
    "populate_isolated_parent_evidence",
    "recovery_authority_fixture",
    "recovery_parent_catalog",
]
