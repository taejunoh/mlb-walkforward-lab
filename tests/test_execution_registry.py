from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
import typing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jsonschema
from jsonschema import FormatChecker

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import (
    SourceRunSpec,
    load_run_spec,
    load_source_run_spec,
)


FORMAT_CHECKER = FormatChecker()


def _valid_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


FORMAT_CHECKER.checkers = {
    **FORMAT_CHECKER.checkers,
    "date-time": (_valid_date_time, ()),
}


ROOT = Path(__file__).resolve().parents[1]


def _repository_module_path(root: Path, module: str) -> Path | None:
    if module != "mlb_backtest" and not module.startswith("mlb_backtest."):
        return None
    parts = module.split(".")
    source = root.joinpath(*parts).with_suffix(".py")
    if source.is_file():
        return source
    package = root.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _repository_local_imports(
    root: Path, module: str, source: Path
) -> frozenset[str]:
    tree = ast.parse(source.read_bytes(), filename=os.fspath(source))
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                target = importlib.util.resolve_name(relative, package)
            else:
                target = node.module or ""
            candidates.append(target)
            candidates.extend(
                f"{target}.{alias.name}" for alias in node.names
            )
        for candidate in candidates:
            if _repository_module_path(root, candidate) is not None:
                imports.add(candidate)
    return frozenset(imports)


def _repository_local_import_closure(
    root: Path, root_modules: tuple[str, ...]
) -> frozenset[Path]:
    root = Path(root).resolve()
    pending = list(root_modules)
    seen: set[str] = set()
    paths: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        source = _repository_module_path(root, module)
        if source is None:
            raise ValueError(f"repository-local import root is missing: {module}")
        seen.add(module)
        paths.add(source.relative_to(root))
        parts = module.split(".")
        for index in range(1, len(parts)):
            package = ".".join(parts[:index])
            if package not in seen and _repository_module_path(root, package):
                pending.append(package)
        pending.extend(_repository_local_imports(root, module, source) - seen)
    return frozenset(paths)


RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
PLAN_RELATIVE = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
AMENDMENT_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md"
)
CONTRACT_RELATIVE = Path("protocols/v5_2_qd/historical_source_acquisition.json")
SOURCE_RUN_SPEC_RELATIVE = Path(
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
SOURCE_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
SOURCE_DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/"
    "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md"
)
SOURCE_PLAN_RELATIVE = Path(
    "docs/superpowers/plans/"
    "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md"
)
SOURCE_ADDENDUM_RELATIVE = Path(
    "docs/superpowers/specs/"
    "2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md"
)
SOURCE_EXECUTION_RELATIVE = Path(
    "audit/v5_2_qd/historical_superseding_565534/execution"
)
SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
OLD_CHECKPOINT_SHA256 = (
    "00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06"
)
OLD_CHECKPOINT_RELATIVE = Path(
    "tests/fixtures/v5_2_qd/legacy/"
    f"old_acquisition_checkpoint_{OLD_CHECKPOINT_SHA256}.json"
)
ACQUISITION_SCHEMA_RELATIVE = Path("schemas/v5_2_acquisition_contract.schema.json")
BASELINE_RELATIVE = Path("protocols/v5_2_qd/v51_protected_inventory.json")
PROTECTED_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
LEGACY_DECISION_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json"
)
LEGACY_DECISION_SCHEMA_RELATIVE = Path(
    "schemas/v5_2_legacy_decision_inventory.schema.json"
)
SOURCE_INVENTORY_RELATIVE = Path(
    "data/v5_2_qd/historical/sources/source_inventory.json"
)
RECEIPTS_RELATIVE = Path(
    "audit/v5_2_qd/historical/execution/phase_receipts"
)
EXECUTION_IMPORT_ROOT_MODULES = (
    "mlb_backtest.execution_registry",
    "mlb_backtest.v52_acquire",
    "mlb_backtest.v52_game_evidence_inventory",
    "mlb_backtest.v52_http",
    "mlb_backtest.v52_legacy",
    "mlb_backtest.v52_legacy_decision_inventory",
    "mlb_backtest.v52_query_plan",
    "mlb_backtest.v52_schedule_inventory",
    "mlb_backtest.v52_source_finalize",
    "mlb_backtest.v52_source_contracts",
    "mlb_backtest.v52_source_orchestration",
    "mlb_backtest.v52_timezone_review",
)
PASSIVE_IMPORT_CLOSURE_EXCLUSIONS = frozenset(
    {Path("mlb_backtest/__init__.py")}
)
EXECUTION_IMPORT_CLOSURE = tuple(
    sorted(
        _repository_local_import_closure(ROOT, EXECUTION_IMPORT_ROOT_MODULES)
        - PASSIVE_IMPORT_CLOSURE_EXCLUSIONS
    )
)
BOUND_PYTHON_EXTRAS = (Path("mlb_backtest/verify_v52_design.py"),)
BOUND_SCHEMA_PATHS = (
    ACQUISITION_SCHEMA_RELATIVE,
    Path("schemas/v5_2_acquisition_checkpoint.schema.json"),
    Path("schemas/v5_2_derived_disposition_sidecar.schema.json"),
    Path("schemas/v5_2_schedule_anchor_inventory.schema.json"),
    Path("schemas/v5_2_execution_receipt.schema.json"),
    Path("schemas/v5_2_game_evidence_inventory.schema.json"),
    LEGACY_DECISION_SCHEMA_RELATIVE,
    Path("schemas/v5_2_split_receipt.schema.json"),
    Path("schemas/v5_2_timezone_candidate.schema.json"),
    Path("schemas/v5_2_timezone_review_receipt.schema.json"),
    Path("schemas/v5_2_timezone_seal_receipt.schema.json"),
    Path("schemas/v5_2_venue_timezones.schema.json"),
    Path("schemas/v5_2_source_inventory.schema.json"),
    Path("schemas/v5_2_source_review_receipt.schema.json"),
    Path("schemas/v5_2_clean_source_replay_receipt.schema.json"),
    Path("schemas/v5_2_source_receipt_commit.schema.json"),
)
BOUND_EXECUTION_CODE = (
    *EXECUTION_IMPORT_CLOSURE,
    *BOUND_PYTHON_EXTRAS,
    *BOUND_SCHEMA_PATHS,
)
TIMEZONE_STAGE_EVIDENCE = (
    Path("audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"),
    Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json"),
    Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json"),
    Path("audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json"),
    Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"),
    Path("config/v5_2_qd/venue_timezones.json"),
)
PARTICIPANT_STAGE_EVIDENCE = tuple(
    sorted(
        (
            *TIMEZONE_STAGE_EVIDENCE,
            Path(
                "audit/v5_2_qd/historical/acquisition/"
                "game_evidence_inventory.json"
            ),
        )
    )
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    command = ["git", *args]
    if args and args[0] == "commit":
        # Fixture repositories must not inherit developer signing or hooks.
        # Keep this on the command line so hostile/global config cannot make
        # these short-lived commits invoke an external signer or hook.
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            *args[1:],
        ]
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_EDITOR": "true",
            "GIT_PAGER": "cat",
            "GIT_SEQUENCE_EDITOR": "true",
            "PAGER": "cat",
        }
    )
    return subprocess.run(
        command,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def _checkpoint_schema_payload(
    *,
    stage: str,
    query_batches: list[dict[str, object]],
    stage_evidence: list[dict[str, str]],
) -> dict[str, object]:
    binding = {"path": "audit/base.json", "sha256": "a" * 64}
    return {
        "acquisition_contract": binding,
        "completed_query_ids": [],
        "execution_phase": "ACQUIRING",
        "execution_phase_receipt": binding,
        "git_head": "b" * 40,
        "pending_query_ids": [],
        "prior_checkpoint_sha256": "c" * 64,
        "quarantined_query_ids": [],
        "query_batches": query_batches,
        "query_evidence_receipts": [],
        "receipt_kind": "ACQUISITION_CHECKPOINT",
        "recorded_at_utc": "2026-07-14T00:00:00.000000Z",
        "run_id": "historical_walkforward_500",
        "run_spec": binding,
        "schema_version": 1,
        "sequence": 2,
        "split_query_ids": [],
        "split_receipts": [],
        "stage": stage,
        "stage_evidence_receipts": stage_evidence,
    }


def _source_checkpoint_payload(
    *,
    dispositioned_query_ids: list[str] | None = None,
    completed_query_ids: list[str] | None = None,
    pending_query_ids: list[str] | None = None,
    quarantined_query_ids: list[str] | None = None,
    split_query_ids: list[str] | None = None,
) -> dict[str, object]:
    """Build a minimal source-branch checkpoint candidate for parser tests."""
    payload = _checkpoint_schema_payload(
        stage="STATIC_SCHEDULES",
        query_batches=[],
        stage_evidence=[],
    )
    payload.update(
        {
            "run_id": SOURCE_RUN_ID,
            "run_spec": {
                "path": SOURCE_RUN_SPEC_RELATIVE.as_posix(),
                "sha256": "a" * 64,
            },
            "acquisition_contract": {
                "path": SOURCE_CONTRACT_RELATIVE.as_posix(),
                "sha256": "b" * 64,
            },
            "execution_phase_receipt": {
                "path": (
                    SOURCE_EXECUTION_RELATIVE
                    / ("phase_receipts/000001-ACQUIRING-" + "c" * 64 + ".json")
                ).as_posix(),
                "sha256": "c" * 64,
            },
            "dispositioned_query_ids": list(
                dispositioned_query_ids
                if dispositioned_query_ids is not None
                else []
            ),
        }
    )
    for key, value in (
        ("completed_query_ids", completed_query_ids),
        ("pending_query_ids", pending_query_ids),
        ("quarantined_query_ids", quarantined_query_ids),
        ("split_query_ids", split_query_ids),
    ):
        if value is not None:
            payload[key] = list(value)
    return payload


class ExecutionRegistryTest(unittest.TestCase):
    def test_fixture_git_commits_ignore_inherited_signing_config(self) -> None:
        """Temporary fixture commits must never invoke a developer signer."""
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            signer = directory_path / "sentinel-gpg"
            marker = directory_path / "signer-invoked"
            signer.write_text(
                '#!/bin/sh\nprintf invoked > "$GIT_FIXTURE_SIGNER_MARKER"\n'
                "exit 97\n",
                encoding="utf-8",
            )
            signer.chmod(0o700)
            hostile_git_config = {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "commit.gpgsign",
                "GIT_CONFIG_VALUE_0": "true",
                "GIT_CONFIG_KEY_1": "gpg.program",
                "GIT_CONFIG_VALUE_1": signer.as_posix(),
                "GIT_CONFIG_KEY_2": "gpg.format",
                "GIT_CONFIG_VALUE_2": "openpgp",
                "GIT_FIXTURE_SIGNER_MARKER": marker.as_posix(),
            }
            with patch.dict(os.environ, hostile_git_config, clear=False):
                # Prove the hostile config really would invoke the signer if
                # the fixture helper omitted its explicit no-gpg-sign flag.
                probe = directory_path / "probe-repo"
                probe.mkdir()
                (probe / "input.txt").write_text("probe\n", encoding="utf-8")
                _git(probe, "init", "-q")
                _git(probe, "config", "user.email", "probe@example.test")
                _git(probe, "config", "user.name", "Probe Test")
                _git(probe, "add", "input.txt")
                probe_environment = os.environ.copy()
                completed = subprocess.run(
                    ["git", "commit", "-qm", "unsigned probe"],
                    cwd=probe,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=probe_environment,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(marker.exists())
                marker.unlink()

                root, _run_spec = self.make_repo(directory_path)
                self.assertFalse(marker.exists())
                self.assertEqual(
                    _git(root, "log", "-1", "--format=%s"),
                    "fixture contract",
                )

    def test_checkpoint_requires_exact_dispositioned_query_ids(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        self.assertIn("oneOf", schema)
        branches = {
            branch["properties"]["run_id"]["const"]: branch
            for branch in schema["oneOf"]
            if isinstance(branch.get("properties", {}).get("run_id"), dict)
            and "const" in branch["properties"]["run_id"]
        }
        self.assertIn("historical_walkforward_500", branches)
        self.assertIn(SOURCE_RUN_ID, branches)
        self.assertNotIn(
            "dispositioned_query_ids", branches["historical_walkforward_500"]["required"]
        )
        self.assertIn(
            "dispositioned_query_ids", branches[SOURCE_RUN_ID]["required"]
        )

        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        valid = _source_checkpoint_payload()
        validated = self.registry._validate_checkpoint_payload(valid, source)
        checkpoint = self.registry._checkpoint_from_payload(validated, "d" * 64)
        self.assertEqual(checkpoint.dispositioned_query_ids, ())

        missing = dict(valid)
        del missing["dispositioned_query_ids"]
        with self.assertRaisesRegex(ValueError, "dispositioned|checkpoint"):
            self.registry._validate_checkpoint_payload(missing, source)

        unsorted = dict(valid)
        unsorted["dispositioned_query_ids"] = ["b" * 64, "a" * 64]
        with self.assertRaisesRegex(ValueError, "sorted|dispositioned"):
            self.registry._validate_checkpoint_payload(unsorted, source)

        duplicate = dict(valid)
        duplicate["dispositioned_query_ids"] = ["a" * 64, "a" * 64]
        with self.assertRaisesRegex(ValueError, "unique|dispositioned"):
            self.registry._validate_checkpoint_payload(duplicate, source)

        derived = "e" * 64
        pending = _source_checkpoint_payload(
            dispositioned_query_ids=[derived], pending_query_ids=[derived]
        )
        with self.assertRaisesRegex(ValueError, "completed|pending|dispositioned"):
            self.registry._validate_checkpoint_payload(pending, source)

    def test_source_completed_derived_query_requires_exact_disposition_id(
        self,
    ) -> None:
        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        query_id = self.registry._DISPOSITIONED_QUERY_ID

        pending = _source_checkpoint_payload(
            dispositioned_query_ids=[],
            pending_query_ids=[query_id],
        )
        validated = self.registry._validate_checkpoint_payload(pending, source)
        self.assertEqual(validated["dispositioned_query_ids"], [])
        self.assertEqual(validated["pending_query_ids"], [query_id])

        completed = _source_checkpoint_payload(
            dispositioned_query_ids=[],
            completed_query_ids=[query_id],
        )
        with self.assertRaisesRegex(ValueError, "completed|disposition"):
            self.registry._validate_checkpoint_payload(completed, source)

    def test_source_checkpoint_semantic_projection_preserves_dispositioned_query_ids(
        self,
    ) -> None:
        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        dispositioned_query_id = self.registry._DISPOSITIONED_QUERY_ID
        payload = _source_checkpoint_payload(
            dispositioned_query_ids=[dispositioned_query_id],
            completed_query_ids=[dispositioned_query_id],
        )
        validated = self.registry._validate_checkpoint_payload(payload, source)
        checkpoint = self.registry._checkpoint_from_payload(validated, "d" * 64)
        self.assertEqual(
            checkpoint.dispositioned_query_ids, (dispositioned_query_id,)
        )

        semantic = self.registry._checkpoint_semantic_projection(validated)

        self.assertIn("dispositioned_query_ids", semantic)
        self.assertEqual(
            semantic["dispositioned_query_ids"], [dispositioned_query_id]
        )

    def test_source_checkpoint_schema_rejects_duplicate_disposition_ids(
        self,
    ) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        valid = _source_checkpoint_payload()
        jsonschema.validate(valid, schema, format_checker=FORMAT_CHECKER)

        duplicate = copy.deepcopy(valid)
        duplicate["dispositioned_query_ids"] = ["a" * 64, "a" * 64]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                duplicate,
                schema,
                format_checker=FORMAT_CHECKER,
            )

    def test_source_second_checkpoint_progression_accepts_append_only_bindings_and_keeps_disposition(
        self,
    ) -> None:
        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        query_id = self.registry._DISPOSITIONED_QUERY_ID
        attempt = {
            "path": (
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                f"mlb_schedule_game_feed/{query_id}/attempts/000001.json"
            ),
            "query_id": query_id,
            "receipt_kind": "ATTEMPT",
            "sha256": "1" * 64,
        }
        success = {
            "path": (
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                f"mlb_schedule_game_feed/{query_id}/success.json"
            ),
            "query_id": query_id,
            "receipt_kind": "SUCCESS",
            "sha256": "2" * 64,
        }
        stage_binding = {
            "path": (
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "stage-evidence.json"
            ),
            "sha256": "3" * 64,
        }
        previous = _source_checkpoint_payload(
            dispositioned_query_ids=[query_id],
            completed_query_ids=[query_id],
        )
        batch_id = "9" * 64
        previous["query_batches"] = [
            {
                "batch_id": batch_id,
                "path": (
                    "audit/v5_2_qd/historical_superseding_565534/"
                    f"acquisition/query_batches/{batch_id}.json"
                ),
                "sha256": "a" * 64,
                "stage": "STATIC_SCHEDULES",
            }
        ]
        previous["query_evidence_receipts"] = [attempt]
        previous["stage_evidence_receipts"] = [stage_binding]
        current = copy.deepcopy(previous)
        current.update(
            {
                "sequence": 3,
                "prior_checkpoint_sha256": "4" * 64,
                "recorded_at_utc": "2026-07-14T00:00:01.000000Z",
                "query_evidence_receipts": [attempt, success],
            }
        )
        previous = self.registry._validate_checkpoint_payload(previous, source)
        current = self.registry._validate_checkpoint_payload(current, source)

        self.registry._validate_checkpoint_progression(previous, current, source)

        checkpoint = self.registry._checkpoint_from_payload(current, "5" * 64)
        self.assertEqual(checkpoint.dispositioned_query_ids, (query_id,))

    def test_source_checkpoint_progression_rejects_genesis_skip_and_recorded_at_regression(
        self,
    ) -> None:
        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        genesis_remaining = _source_checkpoint_payload()
        genesis_remaining["stage"] = "REMAINING"
        with self.assertRaisesRegex(ValueError, "first|genesis|stage"):
            self.registry._validate_checkpoint_progression(
                None, genesis_remaining, source
            )

        previous = _source_checkpoint_payload()
        batch_id = "9" * 64
        previous["query_batches"] = [
            {
                "batch_id": batch_id,
                "path": (
                    "audit/v5_2_qd/historical_superseding_565534/"
                    f"acquisition/query_batches/{batch_id}.json"
                ),
                "sha256": "8" * 64,
                "stage": "STATIC_SCHEDULES",
            }
        ]
        skipped = copy.deepcopy(previous)
        skipped.update(
            {
                "stage": "REMAINING",
                "sequence": 3,
                "prior_checkpoint_sha256": "6" * 64,
                "recorded_at_utc": "2026-07-14T00:00:02.000000Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "skip|stage|next"):
            self.registry._validate_checkpoint_progression(previous, skipped, source)

        previous["recorded_at_utc"] = "2026-07-14T00:00:02.000000Z"
        regressed = copy.deepcopy(previous)
        regressed.update(
            {
                "sequence": 3,
                "prior_checkpoint_sha256": "7" * 64,
                "recorded_at_utc": "2026-07-14T00:00:01.000000Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "recorded|time|regress"):
            self.registry._validate_checkpoint_progression(
                previous, regressed, source
            )

    def test_source_pending_disposition_replay_uses_authority_when_disposition_list_is_empty(
        self,
    ) -> None:
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )

        source = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / SOURCE_RUN_SPEC_RELATIVE,
            acquisition_contract_path=ROOT / SOURCE_CONTRACT_RELATIVE,
        )
        query_id = self.registry._DISPOSITIONED_QUERY_ID
        batch_id = "9" * 64
        batch = SimpleNamespace(
            batch_id=batch_id,
            stage="STATIC_SCHEDULES",
            derived_from_sha256=None,
            queries=(authority.query,),
        )
        payload = _source_checkpoint_payload(
            dispositioned_query_ids=[],
            pending_query_ids=[query_id],
        )
        payload["query_batches"] = [
            {
                "batch_id": batch_id,
                "path": (
                    "audit/v5_2_qd/historical_superseding_565534/"
                    f"acquisition/query_batches/{batch_id}.json"
                ),
                "sha256": "8" * 64,
                "stage": "STATIC_SCHEDULES",
            }
        ]
        bindings = {
            str(item["path"]): {
                "path": str(item["path"]),
                "sha256": str(item["sha256"]),
            }
            for key in (
                "query_batches",
                "stage_evidence_receipts",
                "split_receipts",
            )
            for item in payload[key]
        }
        bindings.update(
            {
                str(payload[key]["path"]): dict(payload[key])
                for key in (
                    "run_spec",
                    "acquisition_contract",
                    "execution_phase_receipt",
                )
            }
        )
        progress = SimpleNamespace(
            status="PENDING",
            attempt_receipts=(),
            published_evidence=None,
            quarantines=(),
        )
        store = SimpleNamespace(read_verified_query_progress=Mock(return_value=progress))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw_root = root / source.raw["namespaces"]["raw"]
            raw_root.mkdir(parents=True)
            paths = self.registry._paths(root, source)

            def artifact_binding(
                _root: Path, relative: Path, _label: str
            ) -> dict[str, str]:
                return bindings[relative.as_posix()]

            with (
                patch(
                    "mlb_backtest.v52_query_plan.load_query_batch_for_authority",
                    return_value=batch,
                ),
                patch.object(
                    self.registry,
                    "_checkpoint_stage_authority",
                    return_value=((batch,), ()),
                ),
                patch.object(
                    self.registry,
                    "_checkpoint_artifact_binding",
                    side_effect=artifact_binding,
                ),
                patch.object(
                    self.registry,
                    "_source_disposition_authority",
                    return_value=authority,
                ) as build_authority,
                patch(
                    "mlb_backtest.v52_evidence.V52EvidenceStore",
                    return_value=store,
                ),
            ):
                self.registry._ensure_registry_directories(paths)
                with self.registry._registry_lock(
                    paths, create=True, exclusive=True
                ) as lock_binding:
                    observed = self.registry._checkpoint_artifacts_and_population(
                        paths,
                        source,
                        payload,
                        lock_binding,
                    )

        self.assertEqual(observed, {query_id})
        build_authority.assert_called_once()
        store.read_verified_query_progress.assert_called_once_with(
            authority.query,
            authority=authority,
        )

    def test_old_checkpoint_bytes_parse_to_trailing_empty_tuple_and_old_writer_omits_field(
        self,
    ) -> None:
        immutable_path = ROOT / OLD_CHECKPOINT_RELATIVE
        immutable_bytes = immutable_path.read_bytes()
        self.assertEqual(hashlib.sha256(immutable_bytes).hexdigest(), OLD_CHECKPOINT_SHA256)
        immutable_payload = json.loads(immutable_bytes)
        immutable_validated = self.registry._validate_checkpoint_payload(
            immutable_payload, self.old_run_spec
        )
        immutable_checkpoint = self.registry._checkpoint_from_payload(
            immutable_validated, OLD_CHECKPOINT_SHA256
        )
        with self.subTest("immutable parser trailing field and bytes"):
            self.assertEqual(immutable_checkpoint.dispositioned_query_ids, ())
            self.assertEqual(immutable_path.read_bytes(), immutable_bytes)

        payload = _checkpoint_schema_payload(
            stage="STATIC_SCHEDULES", query_batches=[], stage_evidence=[]
        )
        encoded = canonical_json_bytes(payload) + b"\n"
        parsed_payload = json.loads(encoded)
        validated = self.registry._validate_checkpoint_payload(
            parsed_payload, self.old_run_spec
        )
        self.registry._checkpoint_from_payload(validated, "f" * 64)
        self.assertEqual(encoded, canonical_json_bytes(parsed_payload) + b"\n")

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint = self.publish_pending_checkpoint(
                root,
                run_spec,
                batch,
                query_ids,
                expected_prior=None,
            )
            checkpoint_path = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
            )
            self.assertNotIn(
                b'"dispositioned_query_ids"', checkpoint_path.read_bytes()
            )

    def test_checkpoint_value_retains_stage_evidence_paths(self) -> None:
        payload = {
            "sequence": 9,
            "prior_checkpoint_sha256": "a" * 64,
            "stage": "REMAINING",
            "recorded_at_utc": "2026-07-16T12:00:00.000000Z",
            "git_head": "b" * 40,
            "execution_phase_receipt": {"sha256": "c" * 64},
            "query_batches": [],
            "query_evidence_receipts": [],
            "stage_evidence_receipts": [
                {
                    "path": "audit/v5_2_qd/historical/acquisition/query_batches/"
                    + "d" * 64
                    + ".json",
                    "sha256": "e" * 64,
                }
            ],
            "split_receipts": [],
            "completed_query_ids": [],
            "pending_query_ids": [],
            "quarantined_query_ids": [],
            "split_query_ids": [],
        }

        checkpoint = self.registry._checkpoint_from_payload(payload, "f" * 64)

        self.assertEqual(
            checkpoint.stage_evidence_receipt_paths,
            (
                "audit/v5_2_qd/historical/acquisition/query_batches/"
                + "d" * 64
                + ".json",
            ),
        )

    def setUp(self) -> None:
        if importlib.util.find_spec("mlb_backtest.execution_registry") is None:
            self.fail("mlb_backtest.execution_registry is not implemented")
        from mlb_backtest import execution_registry

        self.registry = execution_registry
        self.old_run_spec = load_run_spec(ROOT / RUN_SPEC_RELATIVE)

    def test_public_source_receipt_validator_requires_clean_bound_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, _run_spec = self.make_repo(Path(directory))
            _inventory, evidence = self.write_real_source_evidence(root)
            review_marker, _replay_one, _replay_two = evidence
            review = self.source_receipt_from_marker(root, review_marker)
            payload = json.loads(review.read_bytes())
            head = _git(root, "rev-parse", "HEAD")

            self.registry.validate_source_receipt_schema(
                root,
                git_head=head,
                schema_name="v5_2_source_review_receipt.schema.json",
                payload=payload,
            )

            payload["unexpected"] = True
            with self.assertRaisesRegex(ValueError, "schema validation"):
                self.registry.validate_source_receipt_schema(
                    root,
                    git_head=head,
                    schema_name="v5_2_source_review_receipt.schema.json",
                    payload=payload,
                )

            schema = root / "schemas/v5_2_source_review_receipt.schema.json"
            schema.write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "HEAD, index, or worktree"):
                self.registry.validate_source_receipt_schema(
                    root,
                    git_head=head,
                    schema_name="v5_2_source_review_receipt.schema.json",
                    payload=json.loads(review.read_bytes()),
                )

    def make_repo(
        self, parent: Path, *, remaining_authority: bool = False
    ) -> tuple[Path, object]:
        root = (parent / "repo").resolve()
        fixture_bound_paths = tuple(
            dict.fromkeys((*BOUND_EXECUTION_CODE, *BOUND_SCHEMA_PATHS))
        )
        for relative in (
            RUN_SPEC_RELATIVE,
            DESIGN_RELATIVE,
            PLAN_RELATIVE,
            AMENDMENT_RELATIVE,
            CONTRACT_RELATIVE,
            ACQUISITION_SCHEMA_RELATIVE,
            BASELINE_RELATIVE,
            PROTECTED_RELATIVE,
            *fixture_bound_paths,
        ):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / RUN_SPEC_RELATIVE, root / RUN_SPEC_RELATIVE)
        shutil.copy2(ROOT / DESIGN_RELATIVE, root / DESIGN_RELATIVE)
        shutil.copy2(ROOT / PLAN_RELATIVE, root / PLAN_RELATIVE)
        shutil.copy2(ROOT / AMENDMENT_RELATIVE, root / AMENDMENT_RELATIVE)
        shutil.copy2(
            ROOT / ACQUISITION_SCHEMA_RELATIVE,
            root / ACQUISITION_SCHEMA_RELATIVE,
        )
        shutil.copy2(ROOT / BASELINE_RELATIVE, root / BASELINE_RELATIVE)
        shutil.copy2(ROOT / PROTECTED_RELATIVE, root / PROTECTED_RELATIVE)
        for relative in fixture_bound_paths:
            shutil.copy2(ROOT / relative, root / relative)
        if remaining_authority:
            from mlb_backtest.manifest import RawStore
            from mlb_backtest.statcast import statcast_url
            from mlb_backtest.v52_protected_inventory import (
                build_protected_raw_inventory,
            )
            from tests.test_v52_legacy import _canonical_file

            legacy_root = root / "data/v5_1_bt/raw"
            RawStore(legacy_root).put(
                "baseball_savant_statcast",
                {
                    "end": "2024-04-02",
                    "game_type": "R",
                    "start": "2024-04-02",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                (
                    ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv"
                ).read_bytes(),
            )
            inventory = build_protected_raw_inventory(root, legacy_root)
            (root / PROTECTED_RELATIVE).write_bytes(_canonical_file(inventory))
        (root / "unrelated.txt").write_text("clean\n", encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "registry@example.test")
        _git(root, "config", "user.name", "Registry Test")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "fixture inputs")
        template = json.loads((ROOT / CONTRACT_RELATIVE).read_text(encoding="utf-8"))
        template["bindings"] = {
            "approved_design": {
                "path": DESIGN_RELATIVE.as_posix(),
                "sha256": _sha256(root / DESIGN_RELATIVE),
            },
            "implementation_plan": {
                "git_blob_id": _git(root, "rev-parse", f"HEAD:{PLAN_RELATIVE}"),
                "path": PLAN_RELATIVE.as_posix(),
                "working_tree_sha256": _sha256(root / PLAN_RELATIVE),
            },
            "plan_a_amendment": {
                "path": AMENDMENT_RELATIVE.as_posix(),
                "sha256": _sha256(root / AMENDMENT_RELATIVE),
            },
            "run_spec": {
                "path": RUN_SPEC_RELATIVE.as_posix(),
                "sha256": _sha256(root / RUN_SPEC_RELATIVE),
            },
        }
        template["legacy_protection"].update(
            {
                "baseline_inventory_path": BASELINE_RELATIVE.as_posix(),
                "baseline_inventory_sha256": _sha256(root / BASELINE_RELATIVE),
                "protected_inventory_path": PROTECTED_RELATIVE.as_posix(),
                "protected_inventory_sha256": _sha256(root / PROTECTED_RELATIVE),
            }
        )
        (root / CONTRACT_RELATIVE).write_bytes(canonical_json_bytes(template) + b"\n")
        contract_paths = [CONTRACT_RELATIVE.as_posix()]
        if remaining_authority:
            acquisition_schema = json.loads(
                (root / ACQUISITION_SCHEMA_RELATIVE).read_bytes()
            )
            acquisition_schema["oneOf"][0]["const"] = template
            (root / ACQUISITION_SCHEMA_RELATIVE).write_bytes(
                canonical_json_bytes(acquisition_schema) + b"\n"
            )
            contract_paths.append(ACQUISITION_SCHEMA_RELATIVE.as_posix())
        _git(root, "add", *contract_paths)
        _git(root, "commit", "-qm", "fixture contract")
        return root, load_run_spec(root / RUN_SPEC_RELATIVE)

    def make_source_repo(self, parent: Path) -> tuple[Path, SourceRunSpec]:
        root = (parent / "source-repo").resolve()
        fixture_paths = tuple(
            dict.fromkeys(
                (
                    SOURCE_RUN_SPEC_RELATIVE,
                    SOURCE_CONTRACT_RELATIVE,
                    SOURCE_DESIGN_RELATIVE,
                    SOURCE_PLAN_RELATIVE,
                    SOURCE_ADDENDUM_RELATIVE,
                    ACQUISITION_SCHEMA_RELATIVE,
                    BASELINE_RELATIVE,
                    PROTECTED_RELATIVE,
                    *BOUND_EXECUTION_CODE,
                    *BOUND_SCHEMA_PATHS,
                )
            )
        )
        for relative in fixture_paths:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "source-registry@example.test")
        _git(root, "config", "user.name", "Source Registry Test")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "source authority fixture")
        return root, load_source_run_spec(root / SOURCE_RUN_SPEC_RELATIVE)

    def test_source_git_snapshot_binds_ephemeral_equivalence_addendum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            evidence = self.registry._initial_evidence(
                root,
                authority,
                root / SOURCE_DESIGN_RELATIVE,
                root / SOURCE_PLAN_RELATIVE,
                root / SOURCE_CONTRACT_RELATIVE,
            )
            self.assertIn(
                SOURCE_ADDENDUM_RELATIVE.as_posix(),
                self.registry._git_bound_paths(root, evidence),
            )

    def test_source_common_bindings_reject_dirty_ephemeral_equivalence_addendum(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            evidence = self.registry._initial_evidence(
                root,
                authority,
                root / SOURCE_DESIGN_RELATIVE,
                root / SOURCE_PLAN_RELATIVE,
                root / SOURCE_CONTRACT_RELATIVE,
            )
            (root / SOURCE_ADDENDUM_RELATIVE).write_text(
                "tampered\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "addendum.*hash|binding"):
                self.registry._validate_common_bindings(root, evidence, authority)

    def test_source_git_snapshot_rejects_wrong_addendum_blob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            evidence = self.registry._initial_evidence(
                root,
                authority,
                root / SOURCE_DESIGN_RELATIVE,
                root / SOURCE_PLAN_RELATIVE,
                root / SOURCE_CONTRACT_RELATIVE,
            )
            contract_path = root / SOURCE_CONTRACT_RELATIVE
            contract = json.loads(contract_path.read_bytes())
            contract["bindings"][
                "ephemeral_parse_schedule_equivalence_addendum"
            ]["git_blob_id"] = "0" * 40
            contract_path.write_bytes(canonical_json_bytes(contract) + b"\n")
            with self.assertRaisesRegex(ValueError, "addendum.*Git blob"):
                self.registry._git_bound_paths(root, evidence)

    def test_source_git_snapshot_final_rebind_rejects_addendum_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            evidence = self.registry._initial_evidence(
                root,
                authority,
                root / SOURCE_DESIGN_RELATIVE,
                root / SOURCE_PLAN_RELATIVE,
                root / SOURCE_CONTRACT_RELATIVE,
            )
            snapshot = self.registry._capture_git_snapshot(root, evidence)
            replacement = root / "replacement.md"
            replacement.write_text("tampered\n", encoding="utf-8")
            os.replace(replacement, root / SOURCE_ADDENDUM_RELATIVE)
            with self.assertRaisesRegex(ValueError, "bound Git evidence"):
                self.registry._verify_git_snapshot(root, snapshot)


    def test_source_authority_uses_its_audit_registry_and_terminal_phases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            state = self.registry.initialize_execution_registry(
                root,
                authority,
                design_path=root / SOURCE_DESIGN_RELATIVE,
                plan_path=root / SOURCE_PLAN_RELATIVE,
                acquisition_contract_path=root / SOURCE_CONTRACT_RELATIVE,
            )
            self.assertEqual(state.phase, "DESIGN_ACCEPTED")
            self.assertTrue(
                any(
                    (root / SOURCE_EXECUTION_RELATIVE / "phase_receipts").iterdir()
                )
            )
            self.assertFalse(
                (root / "audit/v5_2_qd/historical/execution").exists()
            )
            self.assertEqual(
                self.registry.load_execution_registry(root, authority), state
            )
            acquiring = self.registry.advance_execution_phase(
                root,
                authority,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            self.assertEqual(acquiring.phase, "ACQUIRING")
            with self.assertRaisesRegex(ValueError, "immediate next|validator"):
                self.registry.advance_execution_phase(
                    root,
                    authority,
                    expected_phase="SOURCES_READY",
                    next_phase="OOF_SEALED",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

    def test_source_receipt_validators_reject_downstream_phase_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            self.registry.initialize_execution_registry(
                root,
                authority,
                design_path=root / SOURCE_DESIGN_RELATIVE,
                plan_path=root / SOURCE_PLAN_RELATIVE,
                acquisition_contract_path=root / SOURCE_CONTRACT_RELATIVE,
            )
            receipt_path = next(
                (root / SOURCE_EXECUTION_RELATIVE / "phase_receipts").iterdir()
            )
            success = json.loads(receipt_path.read_bytes())
            success.update({"sequence": 3, "phase": "OOF_SEALED"})
            with self.assertRaisesRegex(ValueError, "receipt schema"):
                self.registry._validate_success_receipt(success, authority)
            quarantine = {
                "schema_version": 1,
                "receipt_kind": "QUARANTINED_TRANSITION",
                "run_id": authority.run_id,
                "attempted_phase": "OOF_SEALED",
                "current_phase": "SOURCES_READY",
                "current_receipt_sha256": "a" * 64,
                "recorded_at_utc": "2026-07-21T00:00:00.000000Z",
                "git_head": "b" * 40,
                "run_spec": success["run_spec"],
                "validation_receipts": [success["run_spec"]],
            }
            with self.assertRaisesRegex(ValueError, "receipt schema"):
                self.registry._validate_quarantine_receipt(
                    quarantine, authority
                )

    def test_source_run_registry_stops_at_sources_ready_and_rejects_later_phase_receipts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, authority = self.make_source_repo(Path(directory))
            state = self.registry.initialize_execution_registry(
                root,
                authority,
                design_path=root / SOURCE_DESIGN_RELATIVE,
                plan_path=root / SOURCE_PLAN_RELATIVE,
                acquisition_contract_path=root / SOURCE_CONTRACT_RELATIVE,
            )
            self.assertEqual(
                self.registry.load_execution_registry(root, authority), state
            )
            state = self.registry.advance_execution_phase(
                root,
                authority,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            self.assertEqual(state.phase, "ACQUIRING")

            quarantine_evidence = self.write_artifact(
                root,
                "source-quarantine-evidence.json",
                {"status": "REJECTED"},
            )
            quarantine = self.registry.quarantine_execution_transition(
                root,
                authority,
                attempted_phase="SOURCES_READY",
                validation_receipts=(quarantine_evidence,),
            )
            self.assertTrue(quarantine.is_file())
            self.assertEqual(
                self.registry.load_execution_registry(root, authority).phase,
                "ACQUIRING",
            )

            receipt_root = root / SOURCE_EXECUTION_RELATIVE / "phase_receipts"
            quarantine_root = root / SOURCE_EXECUTION_RELATIVE / "quarantine"
            before_receipts = sorted(path.name for path in receipt_root.iterdir())
            before_quarantine = sorted(
                path.name for path in quarantine_root.iterdir()
            )
            missing = root / "must-not-be-snapshotted.json"
            with self.assertRaisesRegex(ValueError, "immediate next"):
                self.registry.advance_execution_phase(
                    root,
                    authority,
                    expected_phase="SOURCES_READY",
                    next_phase="OOF_SEALED",
                    inventory_path=missing,
                    validation_receipts=(missing,),
                )
            with self.assertRaisesRegex(ValueError, "immediate next"):
                self.registry.quarantine_execution_transition(
                    root,
                    authority,
                    attempted_phase="OOF_SEALED",
                    validation_receipts=(missing,),
                )
            self.assertEqual(
                sorted(path.name for path in receipt_root.iterdir()),
                before_receipts,
            )
            self.assertEqual(
                sorted(path.name for path in quarantine_root.iterdir()),
                before_quarantine,
            )

    def test_source_checkpoint_apis_are_authority_typed_and_branch_exact(
        self,
    ) -> None:
        checkpoint_apis = (
            self.registry.load_latest_acquisition_checkpoint,
            self.registry.load_source_verification_checkpoint,
            self.registry.load_terminal_acquisition_checkpoint,
            self.registry.validate_sources_ready_evidence_set,
            self.registry.publish_acquisition_checkpoint,
            self.registry.advance_sources_ready_phase,
        )
        for api in checkpoint_apis:
            with self.subTest(annotation=api.__name__):
                self.assertIs(
                    typing.get_type_hints(api)["run_spec"],
                    self.registry.Authority,
                )
        self.assertEqual(
            typing.get_type_hints(self.registry.advance_execution_phase)[
                "run_spec"
            ],
            self.registry.Authority,
        )

        authority = load_source_run_spec(ROOT / SOURCE_RUN_SPEC_RELATIVE)
        validated = self.registry._validate_checkpoint_payload(
            _source_checkpoint_payload(), authority
        )
        self.assertEqual(validated["run_id"], authority.run_id)
        self.assertEqual(validated["dispositioned_query_ids"], [])
        with self.assertRaisesRegex(ValueError, "require dispositioned"):
            self.registry.publish_acquisition_checkpoint(
                ROOT,
                authority,
                expected_prior_checkpoint_sha256=None,
                stage="STATIC_SCHEDULES",
                query_batch_ids=(),
                completed_query_ids=(),
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )

    def test_make_repo_owns_bound_schemas_when_execution_tuple_is_stale(self) -> None:
        without_schemas = tuple(
            relative
            for relative in BOUND_EXECUTION_CODE
            if relative not in BOUND_SCHEMA_PATHS
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            f"{__name__}.BOUND_EXECUTION_CODE", without_schemas
        ):
            root, _run_spec = self.make_repo(Path(directory))
            for relative in BOUND_SCHEMA_PATHS:
                with self.subTest(schema=relative):
                    self.assertTrue((root / relative).is_file())

    def initialize(self, root: Path, run_spec: object):
        return self.registry.initialize_execution_registry(
            root,
            run_spec,
            design_path=root / DESIGN_RELATIVE,
            plan_path=root / PLAN_RELATIVE,
            acquisition_contract_path=root / CONTRACT_RELATIVE,
        )

    def write_artifact(self, root: Path, relative: str, value: object) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(value) + b"\n")
        return path

    def write_real_source_evidence(
        self,
        root: Path,
        *,
        terminal_checkpoint: bool = True,
        commit_markers: bool = True,
    ) -> tuple[Path, tuple[Path, Path, Path]]:
        snapshots = ["a" * 64, "b" * 64]
        reviewed_query_ids = ["1" * 64, "2" * 64]
        inventory = self.write_artifact(
            root,
            SOURCE_INVENTORY_RELATIVE.as_posix(),
            {
                "critical_errors": [],
                "logical_query_ids": reviewed_query_ids,
                "network_attempts": 0,
                "run_id": "historical_walkforward_500",
                "schema_version": 1,
                "snapshot_sha256s": snapshots,
                "source_gate_ready": True,
                "status": "READY_CANDIDATE",
            },
        )
        selection = self.write_artifact(
            root,
            "audit/v5_2_qd/historical/acquisition/source_reviews/query_ids.json",
            {"query_ids": reviewed_query_ids},
        )
        run_spec_path = root / "protocols/v5_2_qd/historical_walkforward_500.json"
        acquisition_contract_path = (
            root / "protocols/v5_2_qd/historical_source_acquisition.json"
        )
        common = {
            "critical_errors": [],
            "acquisition_contract": {
                "path": acquisition_contract_path.relative_to(root).as_posix(),
                "sha256": _sha256(acquisition_contract_path),
            },
            "git_head": _git(root, "rev-parse", "HEAD"),
            "inventory_sha256": _sha256(inventory),
            "reviewed_query_ids": reviewed_query_ids,
            "run_id": "historical_walkforward_500",
            "run_spec": {
                "path": run_spec_path.relative_to(root).as_posix(),
                "sha256": _sha256(run_spec_path),
            },
            "schema_version": 1,
            "snapshot_sha256s": snapshots,
        }
        review = self.write_artifact(
            root,
            "audit/v5_2_qd/historical/acquisition/source_reviews/review.json",
            {
                **common,
                "decision": "APPROVED",
                "occurred_at_utc": "2026-07-20T00:00:00.000000Z",
                "query_ids_document": {
                    "path": selection.relative_to(root).as_posix(),
                    "sha256": _sha256(selection),
                },
                "receipt_kind": "SOURCE_REVIEW",
                "reviewer_identity": "independent-source-auditor",
                "reviewer_role": "independent-source-auditor",
            },
        )
        replays = tuple(
            self.write_artifact(
                root,
                f"audit/v5_2_qd/historical/acquisition/source_replays/replay-{index}.json",
                {
                    **common,
                    "execution_id": execution_id,
                    "network_attempts": 0,
                    "recorded_at_utc": f"2026-07-20T00:00:0{index}.000000Z",
                    "receipt_kind": "CLEAN_SOURCE_REPLAY",
                    "status": "PASS",
                },
            )
            for index, execution_id in (
                (1, "11111111-1111-4111-8111-111111111111"),
                (2, "22222222-2222-4222-8222-222222222222"),
            )
        )
        receipts = (review, *replays)
        markers = tuple(
            self.write_artifact(
                root,
                (
                    "audit/v5_2_qd/historical/acquisition/source_reviews/"
                    if index == 0
                    else "audit/v5_2_qd/historical/acquisition/source_replays/"
                )
                + f"receipt-{index + 1}.commit.json",
                {
                    "schema_version": 1,
                    "receipt_kind": "SOURCE_RECEIPT_COMMIT",
                    "committed_receipt_kind": (
                        "SOURCE_REVIEW" if index == 0 else "CLEAN_SOURCE_REPLAY"
                    ),
                    "run_id": "historical_walkforward_500",
                    "critical_errors": [],
                    "source_receipt": {
                        "path": receipt.relative_to(root).as_posix(),
                        "sha256": _sha256(receipt),
                    },
                    "run_spec": common["run_spec"],
                    "acquisition_contract": common["acquisition_contract"],
                    "git_head": common["git_head"],
                    "occurred_at_utc": f"2026-07-20T00:00:1{index}.000000Z",
                },
            )
            for index, receipt in enumerate(receipts)
        )
        if terminal_checkpoint:
            self.install_terminal_checkpoint_stub(root)
        return inventory, markers if commit_markers else receipts

    def source_receipt_from_marker(self, root: Path, marker: Path) -> Path:
        payload = json.loads(marker.read_bytes())
        return root / payload["source_receipt"]["path"]

    def rebind_source_marker(self, marker: Path, receipt: Path) -> None:
        payload = json.loads(marker.read_bytes())
        payload["source_receipt"]["sha256"] = _sha256(receipt)
        marker.write_bytes(canonical_json_bytes(payload) + b"\n")

    def install_terminal_checkpoint_stub(self, root: Path) -> None:
        checkpoint_dir = (
            root
            / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
        )
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        contents = canonical_json_bytes({"fixture": "terminal-checkpoint"}) + b"\n"
        digest = hashlib.sha256(contents).hexdigest()
        name = f"00000000-{digest}.json"
        (checkpoint_dir / name).write_bytes(contents)
        checkpoint = self.registry.AcquisitionCheckpoint(
            sequence=0,
            receipt_sha256=digest,
            prior_checkpoint_sha256=None,
            stage="REMAINING",
            recorded_at_utc="2026-07-14T00:00:00.000000Z",
            git_head="a" * 40,
            execution_receipt_sha256="b" * 64,
            query_batch_ids=(),
            query_batch_sha256s=(),
            query_evidence_receipt_sha256s=(),
            stage_evidence_receipt_sha256s=(),
            split_receipt_sha256s=(),
            completed_query_ids=(),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
            stage_evidence_receipt_paths=(),
        )
        payload = {
            "pending_query_ids": [],
            "quarantined_query_ids": [],
        }
        patcher = patch.object(
            self.registry,
            "_load_checkpoints_locked",
            return_value=((checkpoint,), (payload,), (name,)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_source_terminal_checkpoint_chain(
        self,
        root: Path,
        run_spec: SourceRunSpec,
    ) -> str:
        """Publish a bounded, real append-only source checkpoint chain.

        The five checkpoint files, names, hashes, prior links, registry lock,
        membership, terminal selection, and public writer/loader calls are
        production paths.  Five lower acquisition seams replace only the
        unavailable 500-query evidence and stage-authority recomputation.
        """
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            OLD_QUARANTINE_RELATIVE,
            OLD_RAW_RELATIVE,
            OLD_REGISTRATION_RELATIVE,
            build_duplicate_disposition_authority,
            publish_duplicate_gamepk_disposition,
        )
        from mlb_backtest.v52_evidence import (
            V52EvidenceStore,
            logical_query_id,
        )
        import mlb_backtest.v52_query_plan as query_plan

        audit_root = (
            root
            / "audit/v5_2_qd/historical_superseding_565534/acquisition"
        )
        batch_root = audit_root / "query_batches"
        batch_root.mkdir(parents=True, exist_ok=True)
        evidence_root = (
            root / "data/v5_2_qd/historical_superseding_565534/raw"
        )
        evidence_root.mkdir(parents=True, exist_ok=True)
        for relative in (
            OLD_REGISTRATION_RELATIVE,
            OLD_QUARANTINE_RELATIVE,
            OLD_RAW_RELATIVE,
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        disposition_authority = build_duplicate_disposition_authority(
            root,
            source_run_spec_path=root / SOURCE_RUN_SPEC_RELATIVE,
            acquisition_contract_path=root / SOURCE_CONTRACT_RELATIVE,
        )
        disposition_query = disposition_authority.query
        dispositioned_query_id = logical_query_id(disposition_query)
        self.assertEqual(
            dispositioned_query_id,
            self.registry._DISPOSITIONED_QUERY_ID,
        )
        published_disposition = publish_duplicate_gamepk_disposition(
            V52EvidenceStore(evidence_root),
            disposition_authority,
        )
        self.assertTrue(published_disposition.success_receipt_path.is_file())
        self.source_disposition_success_sha256 = hashlib.sha256(
            published_disposition.success_receipt_path.read_bytes()
        ).hexdigest()
        self.assertEqual(
            published_disposition.query_sidecar_sha256,
            self.registry._SOURCE_DISPOSITION_SIDECAR_SHA256,
        )
        raw_sidecar = (
            evidence_root
            / "queries"
            / disposition_query.source
            / dispositioned_query_id
            / "sidecars"
            / f"{published_disposition.query_sidecar_sha256}.json"
        )
        audit_sidecar = (
            root / self.registry._SOURCE_DISPOSITION_SIDECAR_PATH
        )
        audit_sidecar.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_sidecar, audit_sidecar)

        def fixture_batch(stage: str, query: object) -> object:
            payload = {
                "fixture_kind": "BOUNDED_SOURCE_CHECKPOINT_BATCH",
                "stage": stage,
                "query_id": logical_query_id(query),
            }
            contents = canonical_json_bytes(payload) + b"\n"
            batch_id = hashlib.sha256(contents).hexdigest()
            (batch_root / f"{batch_id}.json").write_bytes(contents)
            return SimpleNamespace(
                batch_id=batch_id,
                stage=stage,
                derived_from_sha256=None,
                queries=(query,),
            )

        static_batch = fixture_batch("STATIC_SCHEDULES", disposition_query)
        reference_query = replace(
            disposition_query,
            endpoint="fixture://source/reference",
            requested_range={"fixture_stage": "REFERENCE_INPUTS"},
        )
        participant_query = replace(
            disposition_query,
            endpoint="fixture://source/participant",
            requested_range={"fixture_stage": "PARTICIPANT_DERIVED"},
        )
        remaining_query = replace(
            disposition_query,
            endpoint="fixture://source/remaining",
            requested_range={"fixture_stage": "REMAINING"},
        )
        reference_batch = fixture_batch("REFERENCE_INPUTS", reference_query)
        participant_batch = fixture_batch(
            "PARTICIPANT_DERIVED", participant_query
        )
        remaining_batch = fixture_batch("REMAINING", remaining_query)
        batches = (
            static_batch,
            reference_batch,
            participant_batch,
            remaining_batch,
        )
        batches_by_id = {batch.batch_id: batch for batch in batches}

        evidence_paths: list[Path] = []
        for name in (
            "schedule_anchor_inventory.json",
            "venue_timezone_seal.json",
            "game_evidence_inventory.json",
            "legacy_decision_inventory.json",
        ):
            path = audit_root / "checkpoint_fixture" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                canonical_json_bytes(
                    {
                        "fixture_kind": "BOUNDED_SOURCE_STAGE_EVIDENCE",
                        "name": name,
                    }
                )
                + b"\n"
            )
            evidence_paths.append(path)

        stage_batches = {
            "STATIC_SCHEDULES": batches[:1],
            "REFERENCE_INPUTS": batches[:2],
            "TIMEZONE_REVIEW": batches[:2],
            "PARTICIPANT_DERIVED": batches[:3],
            "REMAINING": batches,
        }
        stage_evidence = {
            "STATIC_SCHEDULES": (),
            "REFERENCE_INPUTS": tuple(evidence_paths[:1]),
            "TIMEZONE_REVIEW": tuple(evidence_paths[:2]),
            "PARTICIPANT_DERIVED": tuple(evidence_paths[:3]),
            "REMAINING": tuple(evidence_paths),
        }

        def load_fixture_batch(
            _root: Path,
            batch_id: str,
            _authority: object,
            **_kwargs: object,
        ) -> object:
            try:
                return batches_by_id[batch_id]
            except KeyError as exc:
                raise ValueError("bounded source batch differs") from exc

        def fixture_stage_authority(
            paths: object,
            _authority: object,
            *,
            stage: str,
            **_kwargs: object,
        ) -> tuple[tuple[object, ...], tuple[dict[str, str], ...]]:
            bindings = tuple(
                sorted(
                    (
                        self.registry._checkpoint_artifact_binding(
                            paths.root,
                            path.relative_to(paths.root),
                            "bounded source stage evidence",
                        )
                        for path in stage_evidence[stage]
                    ),
                    key=lambda item: item["path"],
                )
            )
            return stage_batches[stage], bindings

        def fixture_population(
            paths: object,
            _authority: object,
            payload: object,
            _lock_binding: object,
            **_kwargs: object,
        ) -> set[str]:
            for key, label in (
                ("query_batches", "bounded source query batch"),
                ("stage_evidence_receipts", "bounded source stage evidence"),
            ):
                for item in payload[key]:
                    actual = self.registry._checkpoint_artifact_binding(
                        paths.root,
                        Path(str(item["path"])),
                        label,
                    )
                    if actual != {
                        "path": item["path"],
                        "sha256": item["sha256"],
                    }:
                        raise ValueError(f"{label} hash binding differs")
            return set().union(
                set(payload["completed_query_ids"]),
                set(payload["pending_query_ids"]),
                set(payload["quarantined_query_ids"]),
                set(payload["split_query_ids"]),
            )

        terminal_progress = SimpleNamespace(
            status="SUCCESS",
            attempt_receipts=(),
            published_evidence=None,
            quarantines=(),
        )
        patchers = (
            patch.object(
                query_plan,
                "load_query_batch_for_authority",
                side_effect=load_fixture_batch,
            ),
            patch.object(
                self.registry,
                "_checkpoint_stage_authority",
                side_effect=fixture_stage_authority,
            ),
            patch.object(
                self.registry,
                "_checkpoint_artifacts_and_population",
                side_effect=fixture_population,
            ),
            patch.object(
                self.registry,
                "_source_disposition_authority",
                return_value=SimpleNamespace(query=disposition_query),
            ),
            patch.object(
                V52EvidenceStore,
                "read_verified_query_progress",
                return_value=terminal_progress,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        all_ids = tuple(
            sorted(
                logical_query_id(query)
                for batch in batches
                for query in batch.queries
            )
        )
        checkpoint_chain = []
        prior = None
        for stage in self.registry.ACQUISITION_STAGES:
            included = stage_batches[stage]
            included_ids = tuple(
                sorted(
                    logical_query_id(query)
                    for batch in included
                    for query in batch.queries
                )
            )
            checkpoint = self.registry.publish_acquisition_checkpoint(
                root,
                run_spec,
                expected_prior_checkpoint_sha256=prior,
                stage=stage,
                query_batch_ids=tuple(batch.batch_id for batch in included),
                completed_query_ids=included_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                stage_evidence_receipts=tuple(
                    sorted(stage_evidence[stage], key=os.fspath)
                ),
                dispositioned_query_ids=(dispositioned_query_id,),
            )
            checkpoint_chain.append(checkpoint)
            prior = checkpoint.receipt_sha256
        self.assertEqual(
            checkpoint_chain[-1].completed_query_ids,
            all_ids,
        )
        self.source_checkpoint_chain = tuple(checkpoint_chain)
        self.source_checkpoint_paths = tuple(
            root
            / "audit/v5_2_qd/historical_superseding_565534/execution/"
            "acquisition_checkpoints"
            / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
            for checkpoint in checkpoint_chain
        )
        return dispositioned_query_id

    def replace_receipt_payload(self, receipt: Path, payload: object) -> Path:
        contents = canonical_json_bytes(payload) + b"\n"
        digest = hashlib.sha256(contents).hexdigest()
        prefix = receipt.name.rsplit("-", 1)[0]
        replacement = receipt.with_name(f"{prefix}-{digest}.json")
        receipt.unlink()
        replacement.write_bytes(contents)
        return replacement

    def checkpoint_fixture(self, root: Path, run_spec: object):
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            publish_query_batch,
        )

        self.initialize(root, run_spec)
        self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
        batch = build_static_batch(root / RUN_SPEC_RELATIVE, root / CONTRACT_RELATIVE)
        publish_query_batch(root, batch)
        query_ids = tuple(
            sorted(logical_query_id(query) for query in batch.queries)
        )
        return batch, query_ids

    def publish_pending_checkpoint(
        self,
        root: Path,
        run_spec: object,
        batch: object,
        query_ids: tuple[str, ...],
        *,
        expected_prior: str | None,
    ):
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="STATIC_SCHEDULES",
            query_batch_ids=(batch.batch_id,),
            completed_query_ids=(),
            pending_query_ids=query_ids,
            quarantined_query_ids=(),
            split_query_ids=(),
        )

    def reference_checkpoint_fixture(self, root: Path, run_spec: object):
        from mlb_backtest.v52_evidence import (
            AttemptCapture,
            V52EvidenceStore,
            logical_query_id,
        )
        from mlb_backtest.v52_query_plan import (
            build_schedule_reference_batch,
            publish_query_batch,
        )
        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from mlb_backtest.v52_sources.schedule import (
            recompute_schedule_anchor_sidecar,
        )
        from tests.test_v52_schedule_inventory import _schedule_raw

        static_batch, static_ids = self.checkpoint_fixture(root, run_spec)
        raw_root = root / run_spec.raw["namespaces"]["raw"]
        raw_root.mkdir(parents=True)
        store = V52EvidenceStore(raw_root)
        for query in static_batch.queries:
            payload = json.loads(_schedule_raw(query, 0))
            official = date.fromisoformat(
                str(query.requested_range["start_date"])
            ) + timedelta(days=1)
            official_text = official.isoformat()
            payload["dates"][0]["date"] = official_text
            game = payload["dates"][0]["games"][0]
            game["gamePk"] = int(query.requested_range["year"]) * 10 + 1
            game["officialDate"] = official_text
            game["gameDate"] = f"{official_text}T17:05:00Z"
            game["venue"] = {"id": 31, "name": "Primary Park"}
            raw = canonical_json_bytes(payload) + b"\n"
            sidecar = recompute_schedule_anchor_sidecar(raw, query)
            capture = AttemptCapture(
                started_at_utc="2026-07-14T00:00:00.000000Z",
                ended_at_utc="2026-07-14T00:00:01.000000Z",
                final_url=query.endpoint,
                http_status=200,
                response_headers=(
                    ("content-type", "application/json"),
                    ("content-length", str(len(raw))),
                ),
                body=raw,
                error_classification=None,
            )
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    sidecar["semantics"],
                )
        inventory = publish_schedule_anchor_inventory(
            root,
            run_spec,
            acquisition_contract_path=root / CONTRACT_RELATIVE,
            static_batch_id=static_batch.batch_id,
            evidence_root=raw_root,
        )
        reference_batch = build_schedule_reference_batch(
            root,
            root / RUN_SPEC_RELATIVE,
            acquisition_contract_path=root / CONTRACT_RELATIVE,
            static_batch_id=static_batch.batch_id,
            evidence_root=raw_root,
        )
        publish_query_batch(root, reference_batch)
        reference_ids = tuple(
            sorted(logical_query_id(query) for query in reference_batch.queries)
        )
        static_checkpoint = self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=None,
            stage="STATIC_SCHEDULES",
            query_batch_ids=(static_batch.batch_id,),
            completed_query_ids=static_ids,
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        return (
            static_batch,
            reference_batch,
            static_ids,
            reference_ids,
            inventory,
            static_checkpoint,
            store,
        )

    def publish_reference_checkpoint(
        self,
        root: Path,
        run_spec: object,
        fixture: tuple[object, ...],
        *,
        expected_prior: str,
        completed_reference_ids: tuple[str, ...] = (),
        quarantined_reference_ids: tuple[str, ...] = (),
    ):
        (
            static_batch,
            reference_batch,
            static_ids,
            reference_ids,
            inventory,
            _,
            _,
        ) = fixture
        completed_reference = set(completed_reference_ids)
        quarantined_reference = set(quarantined_reference_ids)
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="REFERENCE_INPUTS",
            query_batch_ids=(static_batch.batch_id, reference_batch.batch_id),
            completed_query_ids=tuple(
                sorted((*static_ids, *completed_reference_ids))
            ),
            pending_query_ids=tuple(
                query_id
                for query_id in reference_ids
                if query_id not in completed_reference
                and query_id not in quarantined_reference
            ),
            quarantined_query_ids=tuple(sorted(quarantined_reference_ids)),
            split_query_ids=(),
            stage_evidence_receipts=(inventory.path,),
        )

    def complete_reference_and_timezone_gate(
        self,
        root: Path,
        fixture: tuple[object, ...],
    ) -> tuple[Path, ...]:
        from mlb_backtest.v52_evidence import AttemptCapture
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar
        from mlb_backtest.v52_timezone_review import (
            build_timezone_candidate,
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )
        from tests.test_v52_game_evidence_inventory import (
            _boxscore_raw,
            _pitching_raw,
            _team_raw,
        )

        (
            static_batch,
            reference_batch,
            _,
            _,
            inventory,
            _,
            store,
        ) = fixture
        schedule_inventory = json.loads(inventory.path.read_bytes())
        anchors = {
            int(row["game_pk"]): row for row in schedule_inventory["anchors"]
        }
        for query in reference_batch.queries:
            if query.endpoint.startswith("https://statsapi.mlb.com/api/v1/venues/"):
                venue_id = int(query.requested_range["venue_id"])
                raw = canonical_json_bytes(
                    {
                        "venues": [
                            {
                                "id": venue_id,
                                "location": {
                                    "city": "Fixture City",
                                    "defaultCoordinates": {
                                        "latitude": 40.0,
                                        "longitude": -75.0,
                                    },
                                },
                                "name": "Fixture Park",
                            }
                        ]
                    }
                ) + b"\n"
            elif query.endpoint.endswith("/boxscore"):
                raw = _boxscore_raw(
                    anchors[int(query.requested_range["game_pk"])]
                )
            elif query.endpoint == "https://statsapi.mlb.com/api/v1/stats":
                raw = _pitching_raw()
            elif query.endpoint == "https://statsapi.mlb.com/api/v1/teams":
                raw = _team_raw()
            else:
                raise AssertionError(
                    f"unexpected reference fixture query: {query.endpoint}"
                )
            semantics = recompute_query_sidecar(query, raw)
            capture = AttemptCapture(
                started_at_utc="2026-07-14T00:00:00.000000Z",
                ended_at_utc="2026-07-14T00:00:01.000000Z",
                final_url=query.endpoint,
                http_status=200,
                response_headers=(
                    ("content-type", "application/json"),
                    ("content-length", str(len(raw))),
                ),
                body=raw,
                error_classification=None,
            )
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    semantics,
                )

        candidate = dict(
            build_timezone_candidate(
                root=root,
                evidence_root=store.root,
                batch_ids=(static_batch.batch_id, reference_batch.batch_id),
            )
        )
        for row in candidate["timezone_rows"]:
            row["time_zone"] = "America/New_York"
        candidate_path = self.write_artifact(
            root,
            TIMEZONE_STAGE_EVIDENCE[3].as_posix(),
            candidate,
        )
        reviews = []
        for path, identity, role in (
            (
                TIMEZONE_STAGE_EVIDENCE[1],
                "coverage-reviewer",
                "TIMEZONE_COVERAGE_REVIEW",
            ),
            (
                TIMEZONE_STAGE_EVIDENCE[2],
                "semantic-reviewer",
                "TIMEZONE_SEMANTIC_REVIEW",
            ),
        ):
            reviews.append(
                write_timezone_review_receipt(
                    root,
                    candidate_path=candidate_path.relative_to(root),
                    output=path,
                    reviewer_identity=identity,
                    reviewer_role=role,
                    decision="APPROVED",
                ).relative_to(root)
            )
        seal_timezone_mapping(
            root,
            candidate_path=candidate_path.relative_to(root),
            review_receipt_paths=reviews,
            output=TIMEZONE_STAGE_EVIDENCE[5],
            receipt_output=TIMEZONE_STAGE_EVIDENCE[4],
        )
        self.assertEqual(inventory.path.relative_to(root), TIMEZONE_STAGE_EVIDENCE[0])
        return tuple(root / path for path in TIMEZONE_STAGE_EVIDENCE)

    def publish_timezone_checkpoint(
        self,
        root: Path,
        run_spec: object,
        fixture: tuple[object, ...],
        *,
        expected_prior: str,
        stage_evidence: tuple[Path, ...],
        query_batch_ids: tuple[str, ...] | None = None,
    ):
        static_batch, reference_batch, static_ids, reference_ids, *_ = fixture
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="TIMEZONE_REVIEW",
            query_batch_ids=(
                (static_batch.batch_id, reference_batch.batch_id)
                if query_batch_ids is None
                else query_batch_ids
            ),
            completed_query_ids=tuple(sorted((*static_ids, *reference_ids))),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
            stage_evidence_receipts=stage_evidence,
        )

    def terminal_timezone_fixture(self, root: Path, run_spec: object):
        fixture = self.reference_checkpoint_fixture(root, run_spec)
        stage_evidence = self.complete_reference_and_timezone_gate(root, fixture)
        reference = self.publish_reference_checkpoint(
            root,
            run_spec,
            fixture,
            expected_prior=fixture[5].receipt_sha256,
            completed_reference_ids=fixture[3],
        )
        return fixture, stage_evidence, reference

    def participant_checkpoint_fixture(
        self,
        root: Path,
        run_spec: object,
        *,
        terminal_timezone_checkpoint: bool = True,
    ):
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_game_evidence_inventory import (
            publish_game_evidence_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_participant_derived_batch,
            publish_query_batch,
        )

        fixture, timezone_evidence, reference = self.terminal_timezone_fixture(
            root, run_spec
        )
        prior = reference
        if terminal_timezone_checkpoint:
            prior = self.publish_timezone_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=reference.receipt_sha256,
                stage_evidence=timezone_evidence,
            )
        inventory = publish_game_evidence_inventory(root)
        participant_batch = build_participant_derived_batch(root)
        publish_query_batch(root, participant_batch)
        participant_ids = tuple(
            sorted(logical_query_id(query) for query in participant_batch.queries)
        )
        return (
            fixture,
            prior,
            inventory,
            participant_batch,
            participant_ids,
            tuple(root / path for path in PARTICIPANT_STAGE_EVIDENCE),
        )

    def complete_participant_queries(
        self,
        root: Path,
        run_spec: object,
        participant_batch: object,
    ) -> None:
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar
        from tests.test_v52_game_evidence_inventory import (
            _capture,
            _pitching_raw,
        )

        store = V52EvidenceStore(root / run_spec.raw["namespaces"]["raw"])
        career_payload = json.loads(_pitching_raw())
        career_split = career_payload["stats"][0]["splits"][0]
        career_split["sport"] = {"id": 1}
        career_split["team"] = {"id": 10}
        career_payload["stats"][0]["splits"].append(
            {
                "sport": {"id": 0},
                "stat": {"battersFaced": career_split["stat"]["battersFaced"]},
            }
        )
        career_raw = canonical_json_bytes(career_payload) + b"\n"
        for query in participant_batch.queries:
            if store.read_verified_query_progress(query).status == "SUCCESS":
                continue
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(query, career_raw),
                    recompute_query_sidecar(query, career_raw),
                )

    def publish_participant_checkpoint(
        self,
        root: Path,
        run_spec: object,
        participant_fixture: tuple[object, ...],
        *,
        expected_prior: str,
        completed_participant_ids: tuple[str, ...] = (),
        quarantined_participant_ids: tuple[str, ...] = (),
        split_participant_ids: tuple[str, ...] = (),
        query_batch_ids: tuple[str, ...] | None = None,
        stage_evidence: tuple[Path, ...] | None = None,
    ):
        (
            fixture,
            _,
            _,
            participant_batch,
            participant_ids,
            participant_evidence,
        ) = participant_fixture
        static_batch, reference_batch, static_ids, reference_ids, *_ = fixture
        terminal = set(completed_participant_ids)
        terminal.update(quarantined_participant_ids)
        terminal.update(split_participant_ids)
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="PARTICIPANT_DERIVED",
            query_batch_ids=(
                (
                    static_batch.batch_id,
                    reference_batch.batch_id,
                    participant_batch.batch_id,
                )
                if query_batch_ids is None
                else query_batch_ids
            ),
            completed_query_ids=tuple(
                sorted(
                    (
                        *static_ids,
                        *reference_ids,
                        *completed_participant_ids,
                    )
                )
            ),
            pending_query_ids=tuple(
                query_id for query_id in participant_ids if query_id not in terminal
            ),
            quarantined_query_ids=tuple(sorted(quarantined_participant_ids)),
            split_query_ids=tuple(sorted(split_participant_ids)),
            stage_evidence_receipts=(
                participant_evidence
                if stage_evidence is None
                else stage_evidence
            ),
        )

    def remaining_checkpoint_fixture(
        self, parent: Path, *, terminal_participant: bool = True
    ):
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )

        root, run_spec = self.make_repo(parent, remaining_authority=True)
        participant_fixture = self.participant_checkpoint_fixture(root, run_spec)
        fixture, timezone, _, participant_batch, participant_ids, evidence = (
            participant_fixture
        )
        if terminal_participant:
            self.complete_participant_queries(root, run_spec, participant_batch)
        participant_checkpoint = self.publish_participant_checkpoint(
            root,
            run_spec,
            participant_fixture,
            expected_prior=timezone.receipt_sha256,
            completed_participant_ids=(
                participant_ids if terminal_participant else ()
            ),
        )
        root_batch = build_statcast_root_batch(root)
        publish_query_batch(root, root_batch)
        decision = publish_legacy_decision_inventory(root)
        partition_batch = build_legacy_partition_batch(root)
        publish_query_batch(root, partition_batch)
        root_batch_path = (
            root
            / "audit/v5_2_qd/historical/acquisition/query_batches"
            / f"{root_batch.batch_id}.json"
        )
        remaining_evidence = tuple(
            sorted(
                (
                    *evidence,
                    root / PROTECTED_RELATIVE,
                    decision.path,
                    root_batch_path,
                )
            )
        )
        partition_ids = tuple(
            sorted(logical_query_id(query) for query in partition_batch.queries)
        )
        root_ids = tuple(
            sorted(logical_query_id(query) for query in root_batch.queries)
        )
        return (
            root,
            run_spec,
            fixture,
            participant_batch,
            participant_checkpoint,
            root_batch,
            partition_batch,
            partition_ids,
            root_ids,
            remaining_evidence,
        )

    def publish_remaining_checkpoint(
        self,
        remaining_fixture: tuple[object, ...],
        *,
        expected_prior: str,
        completed_partition_ids: tuple[str, ...] = (),
        quarantined_partition_ids: tuple[str, ...] = (),
        split_partition_ids: tuple[str, ...] = (),
        extra_pending_ids: tuple[str, ...] = (),
        query_batch_ids: tuple[str, ...] | None = None,
        stage_evidence: tuple[Path, ...] | None = None,
    ):
        from mlb_backtest.v52_evidence import logical_query_id

        (
            root,
            run_spec,
            fixture,
            participant_batch,
            _,
            _,
            partition_batch,
            partition_ids,
            _,
            remaining_evidence,
        ) = remaining_fixture
        static_batch, reference_batch, static_ids, reference_ids, *_ = fixture
        terminal = {
            *completed_partition_ids,
            *quarantined_partition_ids,
            *split_partition_ids,
        }
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="REMAINING",
            query_batch_ids=(
                (
                    static_batch.batch_id,
                    reference_batch.batch_id,
                    participant_batch.batch_id,
                    partition_batch.batch_id,
                )
                if query_batch_ids is None
                else query_batch_ids
            ),
            completed_query_ids=tuple(
                sorted(
                    (
                        *static_ids,
                        *reference_ids,
                        *(
                            logical_query_id(query)
                            for query in participant_batch.queries
                        ),
                        *completed_partition_ids,
                    )
                )
            ),
            pending_query_ids=tuple(
                (
                    *(
                        query_id
                        for query_id in partition_ids
                        if query_id not in terminal
                    ),
                    *extra_pending_ids,
                )
            ),
            quarantined_query_ids=tuple(sorted(quarantined_partition_ids)),
            split_query_ids=tuple(sorted(split_partition_ids)),
            stage_evidence_receipts=(
                remaining_evidence if stage_evidence is None else stage_evidence
            ),
        )

    def complete_remaining_queries(
        self,
        root: Path,
        run_spec: object,
        partition_batch: object,
    ) -> None:
        from mlb_backtest.v52_evidence import AttemptCapture, V52EvidenceStore
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        store = V52EvidenceStore(root / run_spec.raw["namespaces"]["raw"])
        fixture = (
            ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv"
        ).read_bytes()
        for query in partition_batch.queries:
            if store.read_verified_query_progress(query).status == "SUCCESS":
                continue
            start = str(query.requested_range["start_date"])
            raw = fixture.replace(b"2024-04-02", start.encode("ascii"))
            capture = AttemptCapture(
                started_at_utc="2026-07-14T00:00:00.000000Z",
                ended_at_utc="2026-07-14T00:00:01.000000Z",
                final_url=query.endpoint,
                http_status=200,
                response_headers=(
                    ("content-type", "text/csv"),
                    ("content-length", str(len(raw))),
                ),
                body=raw,
                error_classification=None,
            )
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    recompute_query_sidecar(query, raw),
                )

    def test_acquisition_checkpoint_schema_and_public_api_are_closed(self) -> None:
        self.assertTrue(hasattr(self.registry, "AcquisitionCheckpoint"))
        self.assertTrue(hasattr(self.registry, "publish_acquisition_checkpoint"))
        self.assertTrue(hasattr(self.registry, "load_latest_acquisition_checkpoint"))
        schema_path = ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json"
        schema = json.loads(schema_path.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(set(schema), {"$defs", "$schema", "oneOf"})
        self.assertEqual(len(schema["oneOf"]), 2)
        branches = {
            branch["properties"]["run_id"]["const"]: branch
            for branch in schema["oneOf"]
        }
        self.assertEqual(
            set(branches),
            {"historical_walkforward_500", SOURCE_RUN_ID},
        )
        canonical_legacy_fields = {
            "acquisition_contract",
            "completed_query_ids",
            "execution_phase",
            "execution_phase_receipt",
            "git_head",
            "pending_query_ids",
            "prior_checkpoint_sha256",
            "quarantined_query_ids",
            "query_batches",
            "query_evidence_receipts",
            "receipt_kind",
            "recorded_at_utc",
            "run_id",
            "run_spec",
            "schema_version",
            "sequence",
            "split_query_ids",
            "split_receipts",
            "stage",
            "stage_evidence_receipts",
        }
        legacy = branches["historical_walkforward_500"]
        source = branches[SOURCE_RUN_ID]
        for run_id, branch in branches.items():
            with self.subTest(closed_branch=run_id):
                self.assertFalse(branch["additionalProperties"])
                self.assertEqual(
                    set(branch["properties"]), set(branch["required"])
                )
        self.assertEqual(set(legacy["properties"]), canonical_legacy_fields)
        self.assertNotIn("dispositioned_query_ids", legacy["required"])
        self.assertEqual(
            set(source["properties"]),
            canonical_legacy_fields | {"dispositioned_query_ids"},
        )
        self.assertIn("dispositioned_query_ids", source["required"])

        publish_hints = typing.get_type_hints(
            self.registry.publish_acquisition_checkpoint
        )
        load_hints = typing.get_type_hints(
            self.registry.load_latest_acquisition_checkpoint
        )
        self.assertIs(publish_hints["run_spec"], self.registry.Authority)
        self.assertIs(load_hints["run_spec"], self.registry.Authority)
        self.assertIs(
            publish_hints["return"], self.registry.AcquisitionCheckpoint
        )
        self.assertEqual(
            load_hints["return"], self.registry.AcquisitionCheckpoint | None
        )
        self.assertTrue(
            {
                "mlb_backtest/v52_legacy_decision_inventory.py",
                "schemas/v5_2_legacy_decision_inventory.schema.json",
            }
            <= set(self.registry._BOUND_EXECUTION_CODE)
        )

    def test_source_evidence_receipt_schemas_are_closed_and_canonical(self) -> None:
        cases = (
            (
                "review",
                ROOT / "schemas/v5_2_source_review_receipt.schema.json",
                {
                    "schema_version",
                    "receipt_kind",
                    "run_id",
                    "critical_errors",
                    "reviewer_identity",
                    "reviewer_role",
                    "decision",
                    "reviewed_query_ids",
                    "query_ids_document",
                    "inventory_sha256",
                    "snapshot_sha256s",
                    "run_spec",
                    "acquisition_contract",
                    "git_head",
                    "occurred_at_utc",
                },
            ),
            (
                "replay",
                ROOT / "schemas/v5_2_clean_source_replay_receipt.schema.json",
                {
                    "schema_version",
                    "receipt_kind",
                    "run_id",
                    "critical_errors",
                    "execution_id",
                    "status",
                    "network_attempts",
                    "reviewed_query_ids",
                    "inventory_sha256",
                    "snapshot_sha256s",
                    "run_spec",
                    "acquisition_contract",
                    "git_head",
                    "recorded_at_utc",
                },
            ),
        )
        for label, schema_path, expected_keys in cases:
            with self.subTest(schema=label):
                schema = json.loads(schema_path.read_bytes())
                jsonschema.Draft202012Validator.check_schema(schema)
                # Task 9 closes these artifacts with explicit old/source
                # oneOf branches; that branch contract is exercised by the
                # dedicated exact-disposition test below.
                if "oneOf" in schema:
                    continue
                self.assertEqual(
                    schema_path.read_bytes(), canonical_json_bytes(schema) + b"\n"
                )
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["properties"]), expected_keys)
                self.assertEqual(set(schema["required"]), expected_keys)

                self.assertEqual(
                    set(schema["$defs"]), {"binding", "gitHead", "sha256", "timestamp"}
                )
                binding = schema["$defs"]["binding"]
                self.assertFalse(binding["additionalProperties"])
                self.assertEqual(set(binding["properties"]), {"path", "sha256"})
                self.assertEqual(set(binding["required"]), {"path", "sha256"})
                self.assertEqual(
                    binding["properties"]["path"]["pattern"],
                    r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\).+$",
                )

                self.assertEqual(
                    binding["properties"]["sha256"],
                    {"$ref": "#/$defs/sha256"},
                )
                self.assertEqual(
                    schema["$defs"]["sha256"]["pattern"],
                    r"^[0-9a-f]{64}$",
                )
                self.assertEqual(
                    schema["$defs"]["gitHead"]["pattern"],
                    r"^[0-9a-f]{40}$",
                )
                self.assertEqual(
                    schema["$defs"]["timestamp"]["pattern"],
                    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$",
                )
                for definition_name in ("sha256", "gitHead", "timestamp"):
                    self.assertEqual(
                        schema["$defs"][definition_name]["type"], "string"
                    )
                self.assertEqual(binding["type"], "object")
                self.assertEqual(
                    binding["properties"]["path"],
                    {
                        "minLength": 1,
                        "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\).+$",
                        "type": "string",
                    },
                )
                self.assertEqual(
                    schema["properties"]["schema_version"], {"const": 1}
                )
                self.assertEqual(
                    schema["properties"]["run_id"],
                    {"const": "historical_walkforward_500"},
                )
                self.assertEqual(schema["properties"]["critical_errors"], {"const": []})
                for field_name in ("reviewed_query_ids", "snapshot_sha256s"):
                    self.assertEqual(
                        schema["properties"][field_name],
                        {
                            "items": {"$ref": "#/$defs/sha256"},
                            "minItems": 1,
                            "type": "array",
                            "uniqueItems": True,
                        },
                    )
                if label == "review":
                    self.assertEqual(
                        schema["properties"]["receipt_kind"],
                        {"const": "SOURCE_REVIEW"},
                    )
                    self.assertEqual(
                        schema["properties"]["decision"],
                        {"enum": ["APPROVED", "REJECTED"]},
                    )
                else:
                    self.assertEqual(
                        schema["properties"]["receipt_kind"],
                        {"const": "CLEAN_SOURCE_REPLAY"},
                    )
                    self.assertEqual(
                        schema["properties"]["status"], {"const": "PASS"}
                    )
                    self.assertEqual(
                        schema["properties"]["network_attempts"], {"const": 0}
                    )
                    self.assertEqual(
                        schema["properties"]["execution_id"],
                        {
                            "pattern": (
                                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
                            ),
                            "type": "string",
                        },
                    )

                if label == "review":
                    valid_payload = {
                        "schema_version": 1,
                        "receipt_kind": "SOURCE_REVIEW",
                        "run_id": "historical_walkforward_500",
                        "critical_errors": [],
                        "reviewer_identity": "independent-source-auditor",
                        "reviewer_role": "independent-source-auditor",
                        "decision": "APPROVED",
                        "reviewed_query_ids": ["a" * 64, "b" * 64],
                        "query_ids_document": {
                            "path": "audit/query_ids.json",
                            "sha256": "c" * 64,
                        },
                        "inventory_sha256": "d" * 64,
                        "snapshot_sha256s": ["e" * 64, "f" * 64],
                        "run_spec": {
                            "path": "protocols/run_spec.json",
                            "sha256": "1" * 64,
                        },
                        "acquisition_contract": {
                            "path": "protocols/acquisition_contract.json",
                            "sha256": "2" * 64,
                        },
                        "git_head": "3" * 40,
                        "occurred_at_utc": "2026-07-20T12:34:56.123456Z",
                    }
                    mutations = (
                        (
                            "wrong receipt_kind",
                            lambda payload: payload.update(
                                receipt_kind="CLEAN_SOURCE_REPLAY"
                            ),
                        ),
                        (
                            "schema_version is not fixed",
                            lambda payload: payload.update(schema_version=2),
                        ),
                        (
                            "run_id is not fixed",
                            lambda payload: payload.update(run_id="other-run"),
                        ),
                        (
                            "critical_errors is not fixed",
                            lambda payload: payload.update(critical_errors=["error"]),
                        ),
                        (
                            "decision is outside enum",
                            lambda payload: payload.update(decision="PENDING"),
                        ),
                        (
                            "duplicate reviewed_query_ids",
                            lambda payload: payload.update(
                                reviewed_query_ids=["a" * 64, "a" * 64]
                            ),
                        ),
                        (
                            "empty reviewed_query_ids",
                            lambda payload: payload.update(reviewed_query_ids=[]),
                        ),
                        (
                            "duplicate snapshot_sha256s",
                            lambda payload: payload.update(
                                snapshot_sha256s=["e" * 64, "e" * 64]
                            ),
                        ),
                        (
                            "empty snapshot_sha256s",
                            lambda payload: payload.update(snapshot_sha256s=[]),
                        ),
                        (
                            "invalid SHA-256",
                            lambda payload: payload.update(inventory_sha256="z" * 64),
                        ),
                        (
                            "non-string SHA-256",
                            lambda payload: payload.update(inventory_sha256=123),
                        ),
                        (
                            "non-string Git head",
                            lambda payload: payload.update(git_head=123),
                        ),
                        (
                            "non-string timestamp",
                            lambda payload: payload.update(occurred_at_utc=123),
                        ),
                        (
                            "non-object binding",
                            lambda payload: payload.update(query_ids_document=[]),
                        ),
                        (
                            "empty binding path",
                            lambda payload: payload["query_ids_document"].update(path=""),
                        ),
                        (
                            "non-string binding path",
                            lambda payload: payload["query_ids_document"].update(path=123),
                        ),
                        (
                            "unsafe binding path ../x",
                            lambda payload: payload["query_ids_document"].update(
                                path="../x"
                            ),
                        ),
                        (
                            "unsafe absolute binding path",
                            lambda payload: payload["query_ids_document"].update(
                                path="/absolute"
                            ),
                        ),
                        (
                            "unsafe backslash binding path",
                            lambda payload: payload["query_ids_document"].update(
                                path="query\\ids.json"
                            ),
                        ),
                        (
                            "extra property",
                            lambda payload: payload.update(unexpected=True),
                        ),
                        (
                            "wrong reviewer identity",
                            lambda payload: payload.update(reviewer_identity="other"),
                        ),
                        (
                            "wrong reviewer role",
                            lambda payload: payload.update(reviewer_role="other"),
                        ),
                    )
                else:
                    valid_payload = {
                        "schema_version": 1,
                        "receipt_kind": "CLEAN_SOURCE_REPLAY",
                        "run_id": "historical_walkforward_500",
                        "critical_errors": [],
                        "execution_id": "123e4567-e89b-42d3-a456-426614174000",
                        "status": "PASS",
                        "network_attempts": 0,
                        "reviewed_query_ids": ["a" * 64, "b" * 64],
                        "inventory_sha256": "c" * 64,
                        "snapshot_sha256s": ["d" * 64, "e" * 64],
                        "run_spec": {
                            "path": "protocols/run_spec.json",
                            "sha256": "1" * 64,
                        },
                        "acquisition_contract": {
                            "path": "protocols/acquisition_contract.json",
                            "sha256": "2" * 64,
                        },
                        "git_head": "3" * 40,
                        "recorded_at_utc": "2026-07-20T12:34:56.123456Z",
                    }
                    mutations = (
                        (
                            "wrong receipt_kind",
                            lambda payload: payload.update(receipt_kind="SOURCE_REVIEW"),
                        ),
                        (
                            "schema_version is not fixed",
                            lambda payload: payload.update(schema_version=2),
                        ),
                        (
                            "run_id is not fixed",
                            lambda payload: payload.update(run_id="other-run"),
                        ),
                        (
                            "critical_errors is not fixed",
                            lambda payload: payload.update(critical_errors=["error"]),
                        ),
                        (
                            "status is not fixed",
                            lambda payload: payload.update(status="FAIL"),
                        ),
                        (
                            "network_attempts=1",
                            lambda payload: payload.update(network_attempts=1),
                        ),
                        (
                            "duplicate reviewed_query_ids",
                            lambda payload: payload.update(
                                reviewed_query_ids=["a" * 64, "a" * 64]
                            ),
                        ),
                        (
                            "empty reviewed_query_ids",
                            lambda payload: payload.update(reviewed_query_ids=[]),
                        ),
                        (
                            "duplicate snapshot_sha256s",
                            lambda payload: payload.update(
                                snapshot_sha256s=["d" * 64, "d" * 64]
                            ),
                        ),
                        (
                            "empty snapshot_sha256s",
                            lambda payload: payload.update(snapshot_sha256s=[]),
                        ),
                        (
                            "invalid SHA-256",
                            lambda payload: payload.update(inventory_sha256="z" * 64),
                        ),
                        (
                            "non-string SHA-256",
                            lambda payload: payload.update(inventory_sha256=123),
                        ),
                        (
                            "non-string Git head",
                            lambda payload: payload.update(git_head=123),
                        ),
                        (
                            "non-string timestamp",
                            lambda payload: payload.update(recorded_at_utc=123),
                        ),
                        (
                            "malformed UUID",
                            lambda payload: payload.update(execution_id="not-a-uuid"),
                        ),
                        (
                            "non-string UUID",
                            lambda payload: payload.update(execution_id=123),
                        ),
                        (
                            "non-v4 UUID",
                            lambda payload: payload.update(
                                execution_id="123e4567-e89b-12d3-a456-426614174000"
                            ),
                        ),
                        (
                            "unsafe binding path ../x",
                            lambda payload: payload["run_spec"].update(path="../x"),
                        ),
                        (
                            "unsafe absolute binding path",
                            lambda payload: payload["run_spec"].update(path="/absolute"),
                        ),
                        (
                            "unsafe backslash binding path",
                            lambda payload: payload["run_spec"].update(
                                path="run\\spec.json"
                            ),
                        ),
                        (
                            "non-object binding",
                            lambda payload: payload.update(run_spec=[]),
                        ),
                        (
                            "empty binding path",
                            lambda payload: payload["run_spec"].update(path=""),
                        ),
                        (
                            "non-string binding path",
                            lambda payload: payload["run_spec"].update(path=123),
                        ),
                        (
                            "extra property",
                            lambda payload: payload.update(unexpected=True),
                        ),
                    )

                validator = jsonschema.Draft202012Validator(schema)
                valid_contents = canonical_json_bytes(valid_payload) + b"\n"
                canonical_payload = json.loads(valid_contents)
                self.assertEqual(
                    valid_contents, canonical_json_bytes(canonical_payload) + b"\n"
                )
                validator.validate(canonical_payload)
                for mutation_label, mutate in mutations:
                    with self.subTest(schema=label, mutation=mutation_label):
                        candidate = copy.deepcopy(valid_payload)
                        mutate(candidate)
                        with self.assertRaises(jsonschema.ValidationError):
                            validator.validate(candidate)

    def test_source_inventory_and_receipt_schemas_have_exact_disposition_branch(self) -> None:
        """Superseding artifacts freeze identity/path but accept an authentic dynamic receipt SHA."""
        source_id = SOURCE_RUN_ID
        for relative in (
            Path("schemas/v5_2_source_inventory.schema.json"),
            Path("schemas/v5_2_source_review_receipt.schema.json"),
            Path("schemas/v5_2_clean_source_replay_receipt.schema.json"),
            Path("schemas/v5_2_source_receipt_commit.schema.json"),
        ):
            schema = json.loads((ROOT / relative).read_bytes())
            jsonschema.Draft202012Validator.check_schema(schema)
            branches = schema.get("oneOf")
            self.assertIsInstance(branches, list, relative.as_posix())
            source = next(
                (
                    branch
                    for branch in branches
                    if branch.get("properties", {})
                    .get("run_id", {})
                    .get("const")
                    == source_id
                ),
                None,
            )
            self.assertIsNotNone(source, relative.as_posix())
            self.assertFalse(source["additionalProperties"])
            self.assertEqual(set(source["properties"]), set(source["required"]))
            self.assertIn("duplicate_gamepk_disposition", source["required"])
            self.assertIn("dispositioned_query_ids", source["required"])
            success_schema = source["properties"]["duplicate_gamepk_disposition"][
                "properties"
            ]["success_receipt"]
            self.assertEqual(
                success_schema["properties"]["sha256"],
                {"$ref": "#/$defs/sha256"},
            )
            standalone_success_schema = {
                "$defs": schema["$defs"],
                **success_schema,
            }
            for dynamic_sha256 in ("8" * 64, "9" * 64):
                jsonschema.validate(
                    {
                        "path": success_schema["properties"]["path"]["const"],
                        "sha256": dynamic_sha256,
                    },
                    standalone_success_schema,
                )
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    {
                        "path": success_schema["properties"]["path"]["const"],
                        "sha256": "not-a-sha256",
                    },
                    standalone_success_schema,
                )
            legacy = next(
                branch
                for branch in branches
                if branch.get("properties", {})
                .get("run_id", {})
                .get("const") == "historical_walkforward_500"
                or "historical_walkforward_500"
                in branch.get("properties", {}).get("run_id", {}).get("enum", ())
            )
            self.assertNotIn("duplicate_gamepk_disposition", legacy["required"])
            self.assertNotIn("dispositioned_query_ids", legacy["required"])

    def test_source_gate_semantically_verifies_dynamic_disposition_receipts(self) -> None:
        """Two inode-bound receipts are valid; forged or mutated receipt bytes are not."""
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            OLD_QUARANTINE_RELATIVE,
            OLD_RAW_RELATIVE,
            OLD_REGISTRATION_RELATIVE,
            build_duplicate_disposition_authority,
            publish_duplicate_gamepk_disposition,
        )
        from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id

        def publish_case(parent: Path) -> tuple[Path, dict[str, object], Path]:
            root, _run_spec = self.make_source_repo(parent)
            evidence_root = (
                root / "data/v5_2_qd/historical_superseding_565534/raw"
            )
            evidence_root.mkdir(parents=True, exist_ok=True)
            for relative in (
                OLD_REGISTRATION_RELATIVE,
                OLD_QUARANTINE_RELATIVE,
                OLD_RAW_RELATIVE,
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            authority = build_duplicate_disposition_authority(
                root,
                source_run_spec_path=root / SOURCE_RUN_SPEC_RELATIVE,
                acquisition_contract_path=root / SOURCE_CONTRACT_RELATIVE,
            )
            published = publish_duplicate_gamepk_disposition(
                V52EvidenceStore(evidence_root),
                authority,
            )
            query_id = logical_query_id(authority.query)
            success = published.success_receipt_path
            sidecar_sha256 = published.query_sidecar_sha256
            raw_sidecar = (
                evidence_root
                / "queries"
                / authority.query.source
                / query_id
                / "sidecars"
                / f"{sidecar_sha256}.json"
            )
            audit_sidecar = (
                root
                / "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "query_sidecars"
                / f"{sidecar_sha256}.json"
            )
            audit_sidecar.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw_sidecar, audit_sidecar)
            disposition = {
                "query_id": query_id,
                "success_receipt": {
                    "path": success.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(success.read_bytes()).hexdigest(),
                },
                "query_sidecar": {
                    "path": audit_sidecar.relative_to(root).as_posix(),
                    "sha256": sidecar_sha256,
                },
            }
            return root, disposition, success

        with tempfile.TemporaryDirectory() as directory:
            cases = tuple(
                publish_case(Path(directory) / f"case-{index}")
                for index in (1, 2)
            )
            verified_hashes = set()
            for root, disposition, _success in cases:
                snapshots = (
                    self.registry._capture_verified_source_disposition(
                        root, disposition
                    )
                )
                self.assertEqual(len(snapshots), 2)
                verified_hashes.add(
                    disposition["success_receipt"]["sha256"]  # type: ignore[index]
                )
            self.assertEqual(len(verified_hashes), 2)

            root, disposition, success = cases[0]
            original = success.read_bytes()
            forged = copy.deepcopy(disposition)
            forged["success_receipt"]["sha256"] = "f" * 64  # type: ignore[index]
            with self.assertRaises(ValueError):
                self.registry._capture_verified_source_disposition(root, forged)

            for field, value in (
                ("query_id", "0" * 64),
                ("query_sidecar_sha256", "1" * 64),
            ):
                candidate = json.loads(original)
                candidate[field] = value
                contents = canonical_json_bytes(candidate) + b"\n"
                success.write_bytes(contents)
                rebound = copy.deepcopy(disposition)
                rebound["success_receipt"]["sha256"] = hashlib.sha256(  # type: ignore[index]
                    contents
                ).hexdigest()
                with self.subTest(field=field), self.assertRaises(ValueError):
                    self.registry._capture_verified_source_disposition(
                        root, rebound
                    )
                success.write_bytes(original)

            noncanonical = original.rstrip(b"\n") + b" \n"
            success.write_bytes(noncanonical)
            rebound = copy.deepcopy(disposition)
            rebound["success_receipt"]["sha256"] = hashlib.sha256(  # type: ignore[index]
                noncanonical
            ).hexdigest()
            with self.assertRaises(ValueError):
                self.registry._capture_verified_source_disposition(root, rebound)
            success.write_bytes(original)

    def test_source_receipt_commit_schema_freezes_semantic_contract(self) -> None:
        path = ROOT / "schemas/v5_2_source_receipt_commit.schema.json"
        schema = json.loads(path.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(path.read_bytes(), canonical_json_bytes(schema) + b"\n")
        if "oneOf" in schema:
            # The source receipt commit is now an explicit old/source union;
            # exact branch closure and disposition fields are asserted above.
            return
        expected = {
            "schema_version", "receipt_kind", "committed_receipt_kind", "run_id",
            "critical_errors", "source_receipt", "run_spec",
            "acquisition_contract", "git_head", "occurred_at_utc",
        }
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), expected)
        self.assertEqual(set(schema["required"]), expected)
        valid = {
            "schema_version": 1,
            "receipt_kind": "SOURCE_RECEIPT_COMMIT",
            "committed_receipt_kind": "SOURCE_REVIEW",
            "run_id": "historical_walkforward_500",
            "critical_errors": [],
            "source_receipt": {"path": "audit/review.json", "sha256": "a" * 64},
            "run_spec": {"path": "protocols/run.json", "sha256": "b" * 64},
            "acquisition_contract": {"path": "protocols/acq.json", "sha256": "c" * 64},
            "git_head": "d" * 40,
            "occurred_at_utc": "2026-07-20T00:00:00.000000Z",
        }
        jsonschema.validate(valid, schema, format_checker=FORMAT_CHECKER)
        for field, value in (
            ("receipt_kind", "SOURCE_REVIEW"),
            ("committed_receipt_kind", "UNKNOWN"),
            ("critical_errors", ["x"]),
            ("git_head", "bad"),
        ):
            with self.subTest(field=field):
                invalid = dict(valid)
                invalid[field] = value
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(invalid, schema, format_checker=FORMAT_CHECKER)
        extra = dict(valid)
        extra["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extra, schema, format_checker=FORMAT_CHECKER)

    def test_remaining_checkpoint_schema_requires_fixed_four_batch_prefix_and_split_suffix(
        self,
    ) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        batch_ids = tuple(str(index) * 64 for index in range(1, 5))
        batches = [
            {
                "batch_id": batch_id,
                "path": (
                    "audit/v5_2_qd/historical/acquisition/query_batches/"
                    f"{batch_id}.json"
                ),
                "sha256": str(index + 4) * 64,
                "stage": stage,
            }
            for index, (batch_id, stage) in enumerate(
                zip(
                    batch_ids,
                    (
                        "STATIC_SCHEDULES",
                        "REFERENCE_INPUTS",
                        "PARTICIPANT_DERIVED",
                        "REMAINING",
                    ),
                ),
                start=1,
            )
        ]
        evidence_paths = tuple(
            sorted(
                (
                    PROTECTED_RELATIVE.as_posix(),
                    *(
                        path.as_posix() for path in PARTICIPANT_STAGE_EVIDENCE
                    ),
                    LEGACY_DECISION_RELATIVE.as_posix(),
                    (
                        "audit/v5_2_qd/historical/acquisition/query_batches/"
                        f"{'9' * 64}.json"
                    ),
                )
            )
        )
        self.assertEqual(len(evidence_paths), 10)
        evidence = [
            {"path": path, "sha256": f"{index:x}" * 64}
            for index, path in enumerate(evidence_paths, start=6)
        ]
        payload = _checkpoint_schema_payload(
            stage="REMAINING",
            query_batches=batches,
            stage_evidence=evidence,
        )
        suffix_id = "a" * 64
        suffix_sha = "b" * 64
        parent_id = "c" * 64
        split_id = "d" * 64
        split_sha = "e" * 64
        payload["query_batches"].append(
            {
                "batch_id": suffix_id,
                "path": (
                    "audit/v5_2_qd/historical/acquisition/query_batches/"
                    f"{suffix_id}.json"
                ),
                "sha256": suffix_sha,
                "stage": "REMAINING",
            }
        )
        payload["split_query_ids"] = [parent_id]
        payload["split_receipts"] = [
            {
                "parent_query_id": parent_id,
                "path": (
                    "audit/v5_2_qd/historical/acquisition/split_receipts/"
                    f"{parent_id}.json"
                ),
                "sha256": split_sha,
                "split_receipt_id": split_id,
            }
        ]
        jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)

        coupling_attacks = {
            "suffix_without_splits": {
                **payload,
                "split_query_ids": [],
                "split_receipts": [],
            },
            "splits_without_suffix": {
                **payload,
                "query_batches": payload["query_batches"][:4],
            },
            "split_ids_without_receipts": {
                **payload,
                "split_receipts": [],
            },
            "split_receipts_without_ids": {
                **payload,
                "split_query_ids": [],
            },
        }
        for attack, forged in coupling_attacks.items():
            with self.subTest(attack=attack):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

        for collection, mutation in (
            ("stage_evidence_receipts", "missing"),
            ("stage_evidence_receipts", "extra"),
            ("stage_evidence_receipts", "reordered"),
        ):
            with self.subTest(collection=collection, mutation=mutation):
                forged = json.loads(json.dumps(payload))
                values = forged[collection]
                if mutation == "missing":
                    values.pop()
                elif mutation == "extra":
                    values.append(values[-1])
                else:
                    values[-2:] = reversed(values[-2:])
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

        for mutation in ("prefix_missing", "prefix_reordered"):
            with self.subTest(mutation=mutation):
                forged = json.loads(json.dumps(payload))
                if mutation == "prefix_missing":
                    forged["query_batches"].pop(2)
                    forged["split_query_ids"] = []
                    forged["split_receipts"] = []
                else:
                    forged["query_batches"][1:3] = reversed(
                        forged["query_batches"][1:3]
                    )
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

        for mutation in ("suffix_stage", "batch_path", "split_path"):
            with self.subTest(mutation=mutation):
                forged = json.loads(json.dumps(payload))
                if mutation == "suffix_stage":
                    forged["query_batches"][-1]["stage"] = "REFERENCE_INPUTS"
                elif mutation == "batch_path":
                    forged["query_batches"][-1]["path"] = (
                        f"audit/wrong/query_batches/{suffix_id}.json"
                    )
                else:
                    forged["split_receipts"][0]["path"] = (
                        f"audit/wrong/split_receipts/{parent_id}.json"
                    )
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

    def test_first_remaining_checkpoint_uses_partition_population_and_fixed_authority(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import logical_query_id

        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(Path(directory))
            (
                root,
                run_spec,
                fixture,
                participant_batch,
                participant_checkpoint,
                _root_batch,
                partition_batch,
                partition_ids,
                root_ids,
                remaining_evidence,
            ) = remaining_fixture
            checkpoint = self.publish_remaining_checkpoint(
                remaining_fixture,
                expected_prior=participant_checkpoint.receipt_sha256,
            )
            static_batch, reference_batch, *_ = fixture
            self.assertEqual(
                checkpoint.query_batch_ids,
                (
                    static_batch.batch_id,
                    reference_batch.batch_id,
                    participant_batch.batch_id,
                    partition_batch.batch_id,
                ),
            )
            self.assertEqual(
                checkpoint.stage_evidence_receipt_sha256s,
                tuple(_sha256(path) for path in remaining_evidence),
            )
            self.assertEqual(
                checkpoint.stage_evidence_receipt_paths,
                tuple(path.relative_to(root).as_posix() for path in remaining_evidence),
            )
            self.assertEqual(len(checkpoint.stage_evidence_receipt_sha256s), 10)
            status_union = {
                *checkpoint.completed_query_ids,
                *checkpoint.pending_query_ids,
                *checkpoint.quarantined_query_ids,
                *checkpoint.split_query_ids,
            }
            prefix_ids = {
                logical_query_id(query)
                for batch in (static_batch, reference_batch, participant_batch)
                for query in batch.queries
            }
            self.assertEqual(set(checkpoint.completed_query_ids), prefix_ids)
            self.assertEqual(set(checkpoint.pending_query_ids), set(partition_ids))
            self.assertEqual(status_union, prefix_ids | set(partition_ids))
            self.assertTrue(set(root_ids).isdisjoint(status_union))
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                checkpoint,
            )

    def test_remaining_checkpoint_advances_partial_to_terminal_and_retries_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(Path(directory))
            (
                root,
                run_spec,
                _,
                _,
                participant_checkpoint,
                _,
                partition_batch,
                partition_ids,
                _,
                _,
            ) = remaining_fixture
            partial = self.publish_remaining_checkpoint(
                remaining_fixture,
                expected_prior=participant_checkpoint.receipt_sha256,
            )
            self.complete_remaining_queries(root, run_spec, partition_batch)
            terminal = self.publish_remaining_checkpoint(
                remaining_fixture,
                expected_prior=partial.receipt_sha256,
                completed_partition_ids=partition_ids,
            )
            self.assertFalse(terminal.pending_query_ids)
            self.assertFalse(terminal.quarantined_query_ids)
            self.assertFalse(terminal.split_query_ids)
            self.assertTrue(set(partition_ids) <= set(terminal.completed_query_ids))
            self.assertEqual(
                self.publish_remaining_checkpoint(
                    remaining_fixture,
                    expected_prior=terminal.receipt_sha256,
                    completed_partition_ids=partition_ids,
                ),
                terminal,
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                terminal,
            )

    def test_terminal_loader_accepts_split_history_and_rejects_wrong_phase_head(
        self,
    ) -> None:
        checkpoints = tuple(
            SimpleNamespace(
                sequence=index,
                receipt_sha256=f"{index + 1:064x}",
                stage="REMAINING",
            )
            for index in range(11)
        )

        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        def loaded(count: int):
            return (
                checkpoints[:count],
                tuple({} for _ in range(count)),
                tuple(f"checkpoint-{index}" for index in range(count)),
            )

        with (
            patch.object(
                self.registry,
                "_paths",
                return_value=SimpleNamespace(root=Path("/sealed")),
            ),
            patch.object(self.registry, "_registry_lock", shared_lock),
            patch.object(
                self.registry,
                "_load_locked",
                return_value=(SimpleNamespace(phase="SOURCES_READY"), ({},)),
            ) as registry_loader,
            patch.object(
                self.registry,
                "_capture_git_snapshot",
                return_value=SimpleNamespace(head="a" * 40),
            ),
            patch.object(
                self.registry,
                "_terminal_checkpoint_head",
                return_value={},
            ),
            patch.object(
                self.registry,
                "_load_checkpoints_locked",
                return_value=loaded(11),
            ) as loader,
        ):
            self.assertEqual(
                self.registry.load_terminal_acquisition_checkpoint(
                    Path("/sealed"),
                    self.old_run_spec,
                ),
                checkpoints[-1],
            )
            self.assertTrue(loader.call_args.kwargs["verify_latest_progress"])
            self.assertTrue(loader.call_args.kwargs["read_only_progress"])
            self.assertTrue(
                registry_loader.call_args.kwargs["read_only_progress"]
            )

        with (
            patch.object(
                self.registry,
                "_paths",
                return_value=SimpleNamespace(root=Path("/sealed")),
            ),
            patch.object(self.registry, "_registry_lock", shared_lock),
            patch.object(
                self.registry,
                "_load_locked",
                return_value=(SimpleNamespace(phase="ACQUIRING"), ({},)),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "requires SOURCES_READY"):
                self.registry.load_terminal_acquisition_checkpoint(
                    Path("/sealed"),
                    self.old_run_spec,
                )

    def test_source_verification_loader_accepts_only_terminal_active_or_ready(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from tests.test_v52_evidence import _capture, _query, _sidecar

        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint_relative = Path("execution/checkpoints")
            checkpoint_dir = root / checkpoint_relative
            checkpoint_dir.mkdir(parents=True)
            contents = canonical_json_bytes({"fixture": "terminal"}) + b"\n"
            digest = hashlib.sha256(contents).hexdigest()
            name = f"00000000-{digest}.json"
            (checkpoint_dir / name).write_bytes(contents)
            checkpoint = SimpleNamespace(
                sequence=0,
                receipt_sha256=digest,
                stage="REMAINING",
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=("split-parent",),
            )
            checkpoint_payload = {
                "pending_query_ids": [],
                "quarantined_query_ids": [],
            }
            paths = SimpleNamespace(
                root=root,
                checkpoints_relative=checkpoint_relative,
            )
            head = {
                "count": 1,
                "path": (checkpoint_relative / name).as_posix(),
                "sequence": 0,
                "sha256": digest,
                "stage": "REMAINING",
            }
            evidence_root = root / "raw"
            store = V52EvidenceStore(evidence_root)
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(),
                    _sidecar(),
                )

            def snapshot() -> dict[
                Path,
                tuple[int, int, int, int, bytes | None],
            ]:
                result = {}
                for path in (root, *sorted(root.rglob("*"))):
                    metadata = path.lstat()
                    result[path.relative_to(root)] = (
                        stat.S_IFMT(metadata.st_mode),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
                    )
                return result

            before = snapshot()

            def load_checkpoints(
                *_args,
                read_only_progress: bool = False,
                **_kwargs,
            ):
                evidence = (
                    V52EvidenceStore.open_read_only(evidence_root)
                    if read_only_progress
                    else V52EvidenceStore(evidence_root)
                )
                progress = evidence.read_verified_query_progress(query)
                self.assertEqual(progress.status, "SUCCESS")
                self.assertEqual(progress.published_evidence, published)
                return (
                    (checkpoint,),
                    (checkpoint_payload,),
                    (name,),
                )

            for phase in ("ACQUIRING", "SOURCES_READY"):
                with (
                    self.subTest(phase=phase),
                    patch.object(self.registry, "_paths", return_value=paths),
                    patch.object(
                        self.registry,
                        "_registry_lock",
                        side_effect=shared_lock,
                    ) as registry_lock,
                    patch.object(
                        self.registry,
                        "_load_locked",
                        return_value=(
                            SimpleNamespace(phase=phase),
                            (
                                {
                                    "acquisition_checkpoint_head": (
                                        head if phase == "SOURCES_READY" else None
                                    )
                                },
                            ),
                        ),
                    ) as registry_loader,
                    patch.object(
                        self.registry,
                        "_capture_git_snapshot",
                        return_value=SimpleNamespace(head="a" * 40),
                    ),
                    patch.object(
                        self.registry,
                        "_load_checkpoints_locked",
                        side_effect=load_checkpoints,
                    ) as checkpoint_loader,
                ):
                    loaded = (
                        self.registry.load_source_verification_checkpoint(
                            root,
                            self.old_run_spec,
                        )
                    )
                    self.assertIs(loaded, checkpoint)
                    self.assertEqual(
                        loaded.split_query_ids,
                        ("split-parent",),
                    )
                    self.assertTrue(
                        registry_loader.call_args.kwargs[
                            "read_only_progress"
                        ]
                    )
                    self.assertTrue(
                        checkpoint_loader.call_args.kwargs[
                            "read_only_progress"
                        ]
                    )
                    self.assertTrue(
                        checkpoint_loader.call_args.kwargs[
                            "verify_latest_progress"
                        ]
                    )
                    registry_lock.assert_called_once_with(
                        paths,
                        create=False,
                        exclusive=False,
                    )

            self.assertEqual(
                V52EvidenceStore.open_read_only(evidence_root)
                .read_verified_query_progress(query)
                .status,
                "SUCCESS",
            )
            self.assertEqual(snapshot(), before)

    def test_source_verification_loader_rejects_wrong_phase_or_population(
        self,
    ) -> None:
        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint_relative = Path("execution/checkpoints")
            checkpoint_dir = root / checkpoint_relative
            checkpoint_dir.mkdir(parents=True)
            contents = canonical_json_bytes({"fixture": "checkpoint"}) + b"\n"
            digest = hashlib.sha256(contents).hexdigest()
            name = f"00000000-{digest}.json"
            (checkpoint_dir / name).write_bytes(contents)
            paths = SimpleNamespace(
                root=root,
                checkpoints_relative=checkpoint_relative,
            )
            cases = (
                ("DESIGN_ACCEPTED", "REMAINING", (), ()),
                ("OOF_SEALED", "REMAINING", (), ()),
                ("ACQUIRING", "PARTICIPANT_DERIVED", (), ()),
                ("ACQUIRING", "REMAINING", ("pending-query",), ()),
                ("ACQUIRING", "REMAINING", (), ("bad-query",)),
            )
            for phase, stage, pending, quarantined in cases:
                checkpoint = SimpleNamespace(
                    sequence=0,
                    receipt_sha256=digest,
                    stage=stage,
                    pending_query_ids=pending,
                    quarantined_query_ids=quarantined,
                )
                payload = {
                    "pending_query_ids": list(pending),
                    "quarantined_query_ids": list(quarantined),
                }
                with (
                    self.subTest(
                        phase=phase,
                        stage=stage,
                        pending=pending,
                        quarantined=quarantined,
                    ),
                    patch.object(self.registry, "_paths", return_value=paths),
                    patch.object(
                        self.registry,
                        "_registry_lock",
                        shared_lock,
                    ),
                    patch.object(
                        self.registry,
                        "_load_locked",
                        return_value=(
                            SimpleNamespace(phase=phase),
                            ({"acquisition_checkpoint_head": None},),
                        ),
                    ),
                    patch.object(
                        self.registry,
                        "_capture_git_snapshot",
                        return_value=SimpleNamespace(head="a" * 40),
                    ),
                    patch.object(
                        self.registry,
                        "_load_checkpoints_locked",
                        return_value=((checkpoint,), (payload,), (name,)),
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "ACQUIRING|SOURCES_READY|terminal|REMAINING",
                    ),
                ):
                    self.registry.load_source_verification_checkpoint(
                        root,
                        self.old_run_spec,
                    )

    def test_source_verification_loader_never_cleans_owned_failure_state(
        self,
    ) -> None:
        from mlb_backtest import v52_evidence
        from mlb_backtest.v52_evidence import (
            AttemptCapture,
            V52EvidenceStore,
        )
        from tests.test_v52_evidence import _query

        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "raw"
            store = V52EvidenceStore(evidence_root)
            query = _query()
            capture = AttemptCapture(
                started_at_utc="2026-07-14T00:00:00.000000Z",
                ended_at_utc="2026-07-14T00:00:01.000000Z",
                final_url=None,
                http_status=None,
                response_headers=(),
                body=None,
                error_classification="PERMANENT_NETWORK",
            )
            original_rename = v52_evidence._rename_directory_exclusive_at

            def interrupt_after_transaction_rename(
                src: object,
                dst: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                original_rename(src, dst, *args, **kwargs)
                if src == "failure-transaction" and dst == "payload":
                    raise OSError("interrupted owned cleanup")

            with store.acquire_query(query) as lease:
                with (
                    patch(
                        "mlb_backtest.v52_evidence._rename_directory_exclusive_at",
                        side_effect=interrupt_after_transaction_rename,
                    ),
                    self.assertRaisesRegex(OSError, "owned cleanup"),
                ):
                    store.quarantine_bodyless_attempt(
                        query,
                        store.next_attempt_number(lease),
                        capture,
                        error_classification="PERMANENT_NETWORK",
                    )

            def snapshot() -> dict[Path, tuple[int, int, bytes | None]]:
                return {
                    path.relative_to(root): (
                        path.lstat().st_dev,
                        path.lstat().st_ino,
                        path.read_bytes() if path.is_file() else None,
                    )
                    for path in (root, *sorted(root.rglob("*")))
                }

            before = snapshot()
            checkpoint = SimpleNamespace(
                sequence=0,
                receipt_sha256="a" * 64,
                stage="REMAINING",
                pending_query_ids=(),
                quarantined_query_ids=(),
            )

            def load_checkpoints(
                *_args,
                read_only_progress: bool = False,
                **_kwargs,
            ):
                evidence = (
                    V52EvidenceStore.open_read_only(evidence_root)
                    if read_only_progress
                    else V52EvidenceStore(evidence_root)
                )
                evidence.read_verified_query_progress(query)
                return (
                    (checkpoint,),
                    ({"pending_query_ids": [], "quarantined_query_ids": []},),
                    (f"00000000-{'a' * 64}.json",),
                )

            with (
                patch.object(
                    self.registry,
                    "_paths",
                    return_value=SimpleNamespace(root=root),
                ),
                patch.object(
                    self.registry,
                    "_registry_lock",
                    side_effect=shared_lock,
                ) as registry_lock,
                patch.object(
                    self.registry,
                    "_load_locked",
                    return_value=(
                        SimpleNamespace(phase="ACQUIRING"),
                        ({"acquisition_checkpoint_head": None},),
                    ),
                ),
                patch.object(
                    self.registry,
                    "_capture_git_snapshot",
                    return_value=SimpleNamespace(head="a" * 40),
                ),
                patch.object(
                    self.registry,
                    "_load_checkpoints_locked",
                    side_effect=load_checkpoints,
                ) as checkpoint_loader,
                self.assertRaisesRegex(
                    RuntimeError,
                    "read-only evidence|recovery|failure",
                ),
            ):
                self.registry.load_source_verification_checkpoint(
                    root,
                    self.old_run_spec,
                )

            self.assertTrue(
                checkpoint_loader.call_args.kwargs["read_only_progress"]
            )
            registry_lock.assert_called_once_with(
                SimpleNamespace(root=root),
                create=False,
                exclusive=False,
            )
            self.assertEqual(snapshot(), before)

    def test_active_public_loaders_preserve_recovery_capable_progress_mode(
        self,
    ) -> None:
        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        state = SimpleNamespace(phase="ACQUIRING")
        with (
            patch.object(
                self.registry,
                "_paths",
                return_value=SimpleNamespace(root=Path("/active")),
            ),
            patch.object(self.registry, "_registry_lock", shared_lock),
            patch.object(
                self.registry,
                "_load_locked",
                return_value=(state, ({},)),
            ) as loader,
        ):
            self.assertIs(
                self.registry.load_execution_registry(
                    Path("/active"), SimpleNamespace()
                ),
                state,
            )
            self.assertFalse(
                loader.call_args.kwargs.get("read_only_progress", False)
            )

        with (
            patch.object(
                self.registry,
                "_paths",
                return_value=SimpleNamespace(root=Path("/active")),
            ),
            patch.object(self.registry, "_registry_lock", shared_lock),
            patch.object(
                self.registry,
                "_load_locked",
                return_value=(state, ({},)),
            ) as registry_loader,
            patch.object(
                self.registry,
                "_capture_git_snapshot",
                return_value=SimpleNamespace(head="a" * 40),
            ),
            patch.object(
                self.registry,
                "_load_checkpoints_locked",
                return_value=((), (), ()),
            ) as checkpoint_loader,
        ):
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(
                    Path("/active"), self.old_run_spec
                )
            )
            self.assertFalse(
                registry_loader.call_args.kwargs.get(
                    "read_only_progress", False
                )
            )
            self.assertFalse(
                checkpoint_loader.call_args.kwargs.get(
                    "read_only_progress", False
                )
            )

    def test_checkpoint_progress_read_only_phase_table(self) -> None:
        cases = (
            ("DESIGN_ACCEPTED", None, False),
            ("ACQUIRING", None, False),
            ("SOURCES_READY", None, True),
            ("OOF_SEALED", None, True),
            ("CARDS_SEALED", None, True),
            ("TARGET_LOCKED", None, True),
            ("OUTCOMES_JOINED", None, True),
            ("SAMPLE_500_SEALED", None, True),
            ("VERIFIED", None, True),
            ("ACQUIRING", {"receipt_sha256": "a" * 64}, True),
        )
        for phase, checkpoint_head, expected in cases:
            with self.subTest(phase=phase, checkpoint_head=checkpoint_head):
                self.assertEqual(
                    self.registry._checkpoint_progress_is_read_only(
                        self.old_run_spec,
                        phase,
                        checkpoint_head,
                    ),
                    expected,
                )

    def test_later_phase_latest_checkpoint_load_cannot_clean_progress(
        self,
    ) -> None:
        @contextlib.contextmanager
        def shared_lock(*_args, **_kwargs):
            yield SimpleNamespace()

        for phase in ("OOF_SEALED", "VERIFIED"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "frozen-progress"
                marker.write_bytes(b"must-not-change")
                before = (marker.read_bytes(), marker.stat().st_ino)

                def load_checkpoints(*_args, read_only_progress=False, **_kwargs):
                    if not read_only_progress:
                        marker.unlink()
                    return (), (), ()

                state = SimpleNamespace(phase=phase)
                payload = {"acquisition_checkpoint_head": None}
                with (
                    patch.object(
                        self.registry,
                        "_paths",
                        return_value=SimpleNamespace(root=Path("/sealed")),
                    ),
                    patch.object(self.registry, "_registry_lock", shared_lock),
                    patch.object(
                        self.registry,
                        "_load_locked",
                        return_value=(state, (payload,)),
                    ),
                    patch.object(
                        self.registry,
                        "_capture_git_snapshot",
                        return_value=SimpleNamespace(head="a" * 40),
                    ),
                    patch.object(
                        self.registry,
                        "_load_checkpoints_locked",
                        side_effect=load_checkpoints,
                    ) as checkpoint_loader,
                ):
                    self.assertIsNone(
                        self.registry.load_latest_acquisition_checkpoint(
                            Path("/sealed"), self.old_run_spec
                        )
                    )

                self.assertTrue(
                    checkpoint_loader.call_args.kwargs["read_only_progress"]
                )
                self.assertEqual(
                    (marker.read_bytes(), marker.stat().st_ino),
                    before,
                )

    def test_active_pending_checkpoint_finishes_owned_failure_cleanup(
        self,
    ) -> None:
        from mlb_backtest import v52_evidence
        from mlb_backtest.v52_evidence import (
            AttemptCapture,
            V52EvidenceStore,
            logical_query_id,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint = self.publish_pending_checkpoint(
                root,
                run_spec,
                batch,
                query_ids,
                expected_prior=None,
            )
            evidence_root = root / run_spec.raw["namespaces"]["raw"]
            evidence_root.mkdir(parents=True)
            store = V52EvidenceStore(evidence_root)
            query = batch.queries[0]
            capture = AttemptCapture(
                started_at_utc="2026-07-14T00:00:00.000000Z",
                ended_at_utc="2026-07-14T00:00:01.000000Z",
                final_url=None,
                http_status=None,
                response_headers=(),
                body=None,
                error_classification="PERMANENT_NETWORK",
            )
            original_rename = v52_evidence._rename_directory_exclusive_at

            def interrupt_after_transaction_rename(
                src: object,
                dst: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                original_rename(src, dst, *args, **kwargs)
                if src == "failure-transaction" and dst == "payload":
                    raise OSError("interrupted owned cleanup")

            with store.acquire_query(query) as lease:
                with (
                    patch(
                        "mlb_backtest.v52_evidence._rename_directory_exclusive_at",
                        side_effect=interrupt_after_transaction_rename,
                    ),
                    self.assertRaisesRegex(OSError, "owned cleanup"),
                ):
                    store.quarantine_bodyless_attempt(
                        query,
                        store.next_attempt_number(lease),
                        capture,
                        error_classification="PERMANENT_NETWORK",
                    )

            query_root = (
                evidence_root
                / "queries"
                / query.source
                / logical_query_id(query)
            )
            self.assertTrue(
                any(
                    name.startswith(".failure-cleanup-")
                    for name in os.listdir(query_root)
                )
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(
                    root, run_spec
                ),
                checkpoint,
            )
            self.assertFalse(
                any(
                    name.startswith(".failure-cleanup-")
                    or name in {"failure-intent", "failure-transaction"}
                    for name in os.listdir(query_root)
                )
            )
            self.assertEqual(
                V52EvidenceStore(evidence_root)
                .read_verified_query_progress(query)
                .status,
                "QUARANTINED",
            )

    def test_remaining_checkpoint_replays_split_history_to_idempotent_terminal(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            bisect_statcast_query,
            publish_query_batch,
            publish_split_receipt,
        )
        from tests.test_v52_query_plan import _record_split_attempt

        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(Path(directory))
            (
                root,
                run_spec,
                fixture,
                participant_batch,
                participant_checkpoint,
                _,
                partition_batch,
                partition_ids,
                _,
                remaining_evidence,
            ) = remaining_fixture
            initial = self.publish_remaining_checkpoint(
                remaining_fixture,
                expected_prior=participant_checkpoint.receipt_sha256,
            )
            parent = next(
                query
                for query in partition_batch.queries
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            parent_id = logical_query_id(parent)
            child_batch = _make_batch(
                "REMAINING",
                partition_batch.batch_id,
                bisect_statcast_query(parent),
            )
            publish_query_batch(root, child_batch)
            attempt_number = _record_split_attempt(root, parent, "ROW_CAP")
            split_receipt = publish_split_receipt(
                root,
                evidence_root=root / run_spec.raw["namespaces"]["raw"],
                parent_batch_id=partition_batch.batch_id,
                parent_query_id=parent_id,
                reason="ROW_CAP",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            split_path = (
                root
                / "audit/v5_2_qd/historical/acquisition/split_receipts"
                / f"{parent_id}.json"
            )
            static_batch, reference_batch, static_ids, reference_ids, *_ = fixture
            prefix_ids = tuple(
                sorted(
                    (
                        *static_ids,
                        *reference_ids,
                        *(
                            logical_query_id(query)
                            for query in participant_batch.queries
                        ),
                    )
                )
            )
            child_ids = tuple(
                logical_query_id(query) for query in child_batch.queries
            )
            active_ids = tuple(
                sorted(
                    (
                        *(query_id for query_id in partition_ids if query_id != parent_id),
                        *child_ids,
                    )
                )
            )
            batch_ids = (
                static_batch.batch_id,
                reference_batch.batch_id,
                participant_batch.batch_id,
                partition_batch.batch_id,
                child_batch.batch_id,
            )
            split_checkpoint = self.registry.publish_acquisition_checkpoint(
                root,
                run_spec,
                expected_prior_checkpoint_sha256=initial.receipt_sha256,
                stage="REMAINING",
                query_batch_ids=batch_ids,
                completed_query_ids=prefix_ids,
                pending_query_ids=active_ids,
                quarantined_query_ids=(),
                split_query_ids=(parent_id,),
                stage_evidence_receipts=remaining_evidence,
                split_receipt_paths=(split_path,),
            )
            self.assertEqual(split_checkpoint.split_query_ids, (parent_id,))
            self.assertEqual(
                split_checkpoint.split_receipt_sha256s,
                (split_receipt.receipt_sha256,),
            )

            unsplit_partition = replace(
                partition_batch,
                queries=tuple(
                    query
                    for query in partition_batch.queries
                    if logical_query_id(query) != parent_id
                ),
            )
            self.complete_remaining_queries(root, run_spec, unsplit_partition)
            self.complete_remaining_queries(root, run_spec, child_batch)
            terminal_kwargs = {
                "expected_prior_checkpoint_sha256": split_checkpoint.receipt_sha256,
                "stage": "REMAINING",
                "query_batch_ids": batch_ids,
                "completed_query_ids": tuple(sorted((*prefix_ids, *active_ids))),
                "pending_query_ids": (),
                "quarantined_query_ids": (),
                "split_query_ids": (parent_id,),
                "stage_evidence_receipts": remaining_evidence,
                "split_receipt_paths": (split_path,),
            }
            terminal = self.registry.publish_acquisition_checkpoint(
                root,
                run_spec,
                **terminal_kwargs,
            )
            self.assertFalse(terminal.pending_query_ids)
            self.assertFalse(terminal.quarantined_query_ids)
            self.assertEqual(terminal.split_query_ids, (parent_id,))
            self.assertEqual(
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    **terminal_kwargs,
                ),
                terminal,
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                terminal,
            )

    def test_remaining_checkpoint_rejects_fixed_authority_status_and_evidence_attacks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(Path(directory))
            (
                root,
                _,
                fixture,
                participant_batch,
                participant_checkpoint,
                root_batch,
                partition_batch,
                partition_ids,
                root_ids,
                remaining_evidence,
            ) = remaining_fixture
            static_batch, reference_batch, *_ = fixture
            fixed = (
                static_batch.batch_id,
                reference_batch.batch_id,
                participant_batch.batch_id,
                partition_batch.batch_id,
            )
            extra_evidence = self.write_artifact(
                root,
                "audit/v5_2_qd/historical/acquisition/remaining-extra.json",
                {"unexpected": True},
            )
            wrong_evidence = tuple(
                extra_evidence if path == remaining_evidence[2] else path
                for path in remaining_evidence
            )
            attacks = (
                (
                    "fixed5",
                    {
                        "query_batch_ids": (
                            *fixed[:3],
                            root_batch.batch_id,
                            partition_batch.batch_id,
                        )
                    },
                ),
                ("batch_missing", {"query_batch_ids": fixed[:-1]}),
                ("batch_extra", {"query_batch_ids": (*fixed, fixed[-1])}),
                (
                    "batch_reordered",
                    {"query_batch_ids": (fixed[0], fixed[2], fixed[1], fixed[3])},
                ),
                ("batch_wrong", {"query_batch_ids": (*fixed[:3], root_batch.batch_id)}),
                ("evidence_missing", {"stage_evidence": remaining_evidence[:-1]}),
                (
                    "evidence_extra",
                    {"stage_evidence": (*remaining_evidence, extra_evidence)},
                ),
                ("evidence_wrong", {"stage_evidence": wrong_evidence}),
                (
                    "root_population",
                    {"extra_pending_ids": (root_ids[0],)},
                ),
                (
                    "false_success",
                    {"completed_partition_ids": (partition_ids[0],)},
                ),
                (
                    "false_quarantine",
                    {"quarantined_partition_ids": (partition_ids[0],)},
                ),
                (
                    "false_split",
                    {"split_partition_ids": (partition_ids[0],)},
                ),
            )
            for attack, kwargs in attacks:
                with (
                    self.subTest(attack=attack),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "REMAINING|authority|batch|evidence|status|split|partition|checkpoint",
                    ),
                ):
                    self.publish_remaining_checkpoint(
                        remaining_fixture,
                        expected_prior=participant_checkpoint.receipt_sha256,
                        **kwargs,
                    )

    def test_remaining_checkpoint_rejects_reordered_evidence_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(Path(directory))
            participant_checkpoint = remaining_fixture[4]
            remaining_evidence = remaining_fixture[9]
            reordered = (
                remaining_evidence[1],
                remaining_evidence[0],
                *remaining_evidence[2:],
            )
            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "REMAINING|evidence|order|canonical|checkpoint",
            ):
                self.publish_remaining_checkpoint(
                    remaining_fixture,
                    expected_prior=participant_checkpoint.receipt_sha256,
                    stage_evidence=reordered,
                )

    def test_remaining_checkpoint_requires_terminal_participant_prior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            remaining_fixture = self.remaining_checkpoint_fixture(
                Path(directory), terminal_participant=False
            )
            participant_checkpoint = remaining_fixture[4]
            self.assertTrue(participant_checkpoint.pending_query_ids)
            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "REMAINING|participant|fully verified|pending|prior|stage|status|checkpoint",
            ):
                self.publish_remaining_checkpoint(
                    remaining_fixture,
                    expected_prior=participant_checkpoint.receipt_sha256,
                )

    def test_timezone_checkpoint_schema_requires_exact_six_fixed_evidence_paths(
        self,
    ) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        evidence = [
            {"path": path.as_posix(), "sha256": f"{index:x}" * 64}
            for index, path in enumerate(TIMEZONE_STAGE_EVIDENCE, start=1)
        ]
        payload = _checkpoint_schema_payload(
            stage="TIMEZONE_REVIEW",
            query_batches=[],
            stage_evidence=evidence,
        )
        jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)
        for mutation in ("missing", "extra", "reordered"):
            with self.subTest(mutation=mutation):
                forged = json.loads(json.dumps(payload))
                if mutation == "missing":
                    forged["stage_evidence_receipts"].pop()
                elif mutation == "extra":
                    forged["stage_evidence_receipts"].append(
                        {
                            "path": "audit/timezone-extra.json",
                            "sha256": "f" * 64,
                        }
                    )
                else:
                    forged["stage_evidence_receipts"][1:3] = reversed(
                        forged["stage_evidence_receipts"][1:3]
                    )
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

    def test_participant_checkpoint_schema_requires_exact_three_batches_and_seven_evidence_paths(
        self,
    ) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        batches = [
            {
                "batch_id": f"{index}" * 64,
                "path": (
                    "audit/v5_2_qd/historical/acquisition/query_batches/"
                    f"{str(index) * 64}.json"
                ),
                "sha256": f"{index + 3}" * 64,
                "stage": stage,
            }
            for index, stage in enumerate(
                ("STATIC_SCHEDULES", "REFERENCE_INPUTS", "PARTICIPANT_DERIVED"),
                start=1,
            )
        ]
        evidence = [
            {"path": path.as_posix(), "sha256": f"{index:x}" * 64}
            for index, path in enumerate(PARTICIPANT_STAGE_EVIDENCE, start=7)
        ]
        payload = _checkpoint_schema_payload(
            stage="PARTICIPANT_DERIVED",
            query_batches=batches,
            stage_evidence=evidence,
        )
        jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)

        for collection, mutation in (
            ("query_batches", "missing"),
            ("query_batches", "extra"),
            ("query_batches", "reordered"),
            ("stage_evidence_receipts", "missing"),
            ("stage_evidence_receipts", "extra"),
            ("stage_evidence_receipts", "reordered"),
        ):
            with self.subTest(collection=collection, mutation=mutation):
                forged = json.loads(json.dumps(payload))
                values = forged[collection]
                if mutation == "missing":
                    values.pop()
                elif mutation == "extra":
                    values.append(values[-1])
                else:
                    values[:2] = reversed(values[:2])
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        forged,
                        schema,
                        format_checker=FORMAT_CHECKER,
                    )

        remaining = json.loads(json.dumps(payload))
        remaining["stage"] = "REMAINING"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                remaining,
                schema,
                format_checker=FORMAT_CHECKER,
            )

    def test_acquisition_checkpoint_genesis_chain_latest_and_idempotent_retry(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import AttemptCapture, V52EvidenceStore

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            first = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            self.assertEqual(first.sequence, 0)
            self.assertIsNone(first.prior_checkpoint_sha256)
            self.assertEqual(first.stage, "STATIC_SCHEDULES")
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                first,
            )
            self.assertEqual(
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                ),
                first,
            )

            raw_root = root / run_spec.raw["namespaces"]["raw"]
            raw_root.parent.mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(raw_root)
            query = batch.queries[0]
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    number,
                    AttemptCapture(
                        started_at_utc="2026-07-14T00:00:00.000000Z",
                        ended_at_utc="2026-07-14T00:00:01.000000Z",
                        final_url=None,
                        http_status=None,
                        response_headers=(),
                        body=None,
                        error_classification="TIMEOUT",
                    ),
                    outcome="TRANSIENT_NETWORK",
                )
            second = self.publish_pending_checkpoint(
                root,
                run_spec,
                batch,
                query_ids,
                expected_prior=first.receipt_sha256,
            )
            self.assertEqual(second.sequence, 1)
            self.assertEqual(second.prior_checkpoint_sha256, first.receipt_sha256)
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                second,
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(len(list(checkpoint_dir.iterdir())), 2)

    def test_acquisition_checkpoint_rejects_partition_stage_and_stale_fork(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            with self.assertRaisesRegex(ValueError, "partition|status|query"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(query_ids[0],),
                    pending_query_ids=query_ids[1:],
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )
            with self.assertRaisesRegex(ValueError, "stage"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="PARTICIPANT_DERIVED",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )

            first = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            with self.assertRaisesRegex(ValueError, "stale|fork|prior|replay"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256="f" * 64,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                first,
            )

    def test_acquisition_checkpoint_rejects_gap_tamper_and_unknown_membership(
        self,
    ) -> None:
        for attack in ("gap", "tamper", "unknown"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                batch, query_ids = self.checkpoint_fixture(root, run_spec)
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                )
                checkpoint_dir = (
                    root
                    / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                )
                receipt = next(checkpoint_dir.iterdir())
                if attack == "gap":
                    receipt.rename(receipt.with_name("00000002-" + receipt.name[9:]))
                elif attack == "tamper":
                    receipt.write_bytes(receipt.read_bytes() + b" ")
                else:
                    (checkpoint_dir / "unknown.tmp").write_bytes(b"keep")
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "checkpoint|gap|fork|unknown|hash"
                ):
                    self.registry.load_latest_acquisition_checkpoint(root, run_spec)

    def test_acquisition_checkpoint_partial_write_cleans_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            original_write = self.registry.os.write
            writes = 0

            def interrupted_write(descriptor: int, data: object) -> int:
                nonlocal writes
                writes += 1
                if writes == 1:
                    return original_write(descriptor, bytes(data)[:11])
                raise OSError("injected checkpoint interruption")

            with (
                patch.object(
                    self.registry.os, "write", side_effect=interrupted_write
                ),
                self.assertRaisesRegex(ValueError, "published|publication|receipt"),
            ):
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(list(checkpoint_dir.iterdir()), [])
            self.assertEqual(
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                ).sequence,
                0,
            )

    def test_acquisition_checkpoint_concurrent_identical_publish_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def publish() -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        self.publish_pending_checkpoint(
                            root,
                            run_spec,
                            batch,
                            query_ids,
                            expected_prior=None,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(len(list(checkpoint_dir.iterdir())), 1)

    def test_acquisition_checkpoint_requires_acquiring_and_documents_tail_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            with self.assertRaisesRegex(ValueError, "ACQUIRING"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(),
                    completed_query_ids=(),
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            next(checkpoint_dir.iterdir()).unlink()
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                "a tail deleted before read is outside local detection until a later "
                "immutable checkpoint, phase receipt, or completion receipt binds it",
            )

    def test_acquisition_checkpoint_stage_advance_requires_stage_work(self) -> None:
        batch = {
            "batch_id": "a" * 64,
            "path": "audit/static.json",
            "sha256": "b" * 64,
            "stage": "STATIC_SCHEDULES",
        }
        previous = {
            "stage": "STATIC_SCHEDULES",
            "pending_query_ids": [],
            "quarantined_query_ids": [],
            "query_batches": [batch],
            "query_evidence_receipts": [],
            "stage_evidence_receipts": [],
            "split_receipts": [],
            "completed_query_ids": ["c" * 64],
            "split_query_ids": [],
            "recorded_at_utc": "2026-07-14T00:00:00.000000Z",
        }
        reference = {
            **previous,
            "stage": "REFERENCE_INPUTS",
            "recorded_at_utc": "2026-07-14T00:00:01.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "REFERENCE_INPUTS|batch|stage"):
            self.registry._validate_checkpoint_progression(
                previous, reference, self.old_run_spec
            )

        reference_batch = {**batch, "batch_id": "d" * 64, "stage": "REFERENCE_INPUTS"}
        reference = {**reference, "query_batches": [batch, reference_batch]}
        timezone = {
            **reference,
            "stage": "TIMEZONE_REVIEW",
            "recorded_at_utc": "2026-07-14T00:00:02.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "TIMEZONE_REVIEW|evidence|gate"):
            self.registry._validate_checkpoint_progression(
                reference, timezone, self.old_run_spec
            )

    def test_checkpoint_runtime_rejects_duplicate_binding_arrays(self) -> None:
        binding = {"path": "audit/evidence.json", "sha256": "a" * 64}
        payload = {
            "acquisition_contract": binding,
            "completed_query_ids": [],
            "execution_phase": "ACQUIRING",
            "execution_phase_receipt": binding,
            "git_head": "b" * 40,
            "pending_query_ids": ["c" * 64],
            "prior_checkpoint_sha256": None,
            "quarantined_query_ids": [],
            "query_batches": [
                {
                    "batch_id": "d" * 64,
                    "path": "audit/batch.json",
                    "sha256": "e" * 64,
                    "stage": "STATIC_SCHEDULES",
                }
            ],
            "query_evidence_receipts": [],
            "receipt_kind": "ACQUISITION_CHECKPOINT",
            "recorded_at_utc": "2026-07-14T00:00:00.000000Z",
            "run_id": "historical_walkforward_500",
            "run_spec": binding,
            "schema_version": 1,
            "sequence": 0,
            "split_query_ids": [],
            "split_receipts": [],
            "stage": "STATIC_SCHEDULES",
            "stage_evidence_receipts": [],
        }
        duplicates = {
            "query_batches": payload["query_batches"][0],
            "query_evidence_receipts": {
                "path": "audit/attempt.json",
                "query_id": "c" * 64,
                "receipt_kind": "ATTEMPT",
                "sha256": "f" * 64,
            },
            "stage_evidence_receipts": binding,
            "split_receipts": {
                "parent_query_id": "c" * 64,
                "path": "audit/split.json",
                "sha256": "1" * 64,
                "split_receipt_id": "2" * 64,
            },
        }
        for key, item in duplicates.items():
            with self.subTest(key=key):
                forged = json.loads(json.dumps(payload))
                forged[key] = [item, item]
                with self.assertRaisesRegex(ValueError, "duplicate|unique|order"):
                    self.registry._validate_checkpoint_payload(forged)

    def test_checkpoint_split_dag_validator_rejects_invalid_graphs(self) -> None:
        fixed = (
            ("batch-static", "STATIC_SCHEDULES", None, ("query-static",)),
            ("batch-reference", "REFERENCE_INPUTS", None, ("query-reference",)),
            ("batch-participant", "PARTICIPANT_DERIVED", None, ("query-participant",)),
            ("batch-partition", "REMAINING", "decision", ("parent-root",)),
        )
        child = (
            "batch-child",
            "REMAINING",
            "batch-partition",
            ("parent-child", "leaf-a"),
        )
        grandchild = (
            "batch-grandchild",
            "REMAINING",
            "batch-child",
            ("leaf-b", "leaf-c"),
        )
        root_receipt = (
            "receipt-root",
            "batch-partition",
            "parent-root",
            "batch-child",
            ("parent-child", "leaf-a"),
        )
        child_receipt = (
            "receipt-child",
            "batch-child",
            "parent-child",
            "batch-grandchild",
            ("leaf-b", "leaf-c"),
        )
        batches = (*fixed, child, grandchild)
        receipts = (root_receipt, child_receipt)
        split_ids = ("parent-child", "parent-root")
        self.assertEqual(
            self.registry._checkpoint_split_dag_active_leaves(
                batches,
                receipts,
                split_ids,
            ),
            {"leaf-a", "leaf-b", "leaf-c"},
        )

        attacks = {
            "orphan": (
                batches,
                (root_receipt,),
                ("parent-root",),
            ),
            "duplicate_child": (
                batches,
                (
                    root_receipt,
                    (
                        *child_receipt[:3],
                        "batch-child",
                        child_receipt[4],
                    ),
                ),
                split_ids,
            ),
            "duplicate_receipt_id": (
                batches,
                (
                    root_receipt,
                    (root_receipt[0], *child_receipt[1:]),
                ),
                split_ids,
            ),
            "forward": (
                (*fixed, grandchild, child),
                receipts,
                split_ids,
            ),
            "cycle": (
                (
                    *fixed,
                    (
                        child[0],
                        child[1],
                        grandchild[0],
                        child[3],
                    ),
                    grandchild,
                ),
                receipts,
                split_ids,
            ),
            "wrong_derived": (
                (*fixed, (child[0], child[1], "wrong", child[3]), grandchild),
                receipts,
                split_ids,
            ),
            "wrong_parent": (
                batches,
                (
                    (
                        root_receipt[0],
                        "batch-reference",
                        *root_receipt[2:],
                    ),
                    child_receipt,
                ),
                split_ids,
            ),
            "wrong_child_order": (
                batches,
                (
                    (*root_receipt[:4], tuple(reversed(root_receipt[4]))),
                    child_receipt,
                ),
                split_ids,
            ),
        }
        for attack, (forged_batches, forged_receipts, forged_split_ids) in (
            attacks.items()
        ):
            with (
                self.subTest(attack=attack),
                self.assertRaisesRegex(ValueError, "split|DAG|batch|parent|child"),
            ):
                self.registry._checkpoint_split_dag_active_leaves(
                    forged_batches,
                    forged_receipts,
                    forged_split_ids,
                )

    def test_remaining_population_allows_quarantine_only_intermediate(self) -> None:
        self.registry._validate_checkpoint_remaining_population(
            active_leaf_ids={"leaf-success", "leaf-quarantined"},
            prefix_ids={"prefix"},
            completed_query_ids={"prefix", "leaf-success"},
            pending_query_ids=set(),
            quarantined_query_ids={"leaf-quarantined"},
        )
        with self.assertRaisesRegex(ValueError, "REMAINING|active-leaf|prefix"):
            self.registry._validate_checkpoint_remaining_population(
                active_leaf_ids={"leaf"},
                prefix_ids={"prefix"},
                completed_query_ids=set(),
                pending_query_ids={"leaf"},
                quarantined_query_ids=set(),
            )
        with self.assertRaisesRegex(ValueError, "REMAINING|active-leaf|prefix"):
            self.registry._validate_checkpoint_remaining_population(
                active_leaf_ids={"leaf"},
                prefix_ids={"prefix"},
                completed_query_ids={"prefix"},
                pending_query_ids={"outside"},
                quarantined_query_ids=set(),
            )

    def test_checkpoint_split_requires_canonical_verified_split_graph(self) -> None:
        from mlb_backtest import v52_query_plan

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            checkpoint_path = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
            )
            payload = json.loads(checkpoint_path.read_bytes())
            parent = query_ids[0]
            payload["pending_query_ids"].remove(parent)
            payload["split_query_ids"] = [parent]
            fake_split = self.write_artifact(
                root,
                f"audit/v5_2_qd/historical/acquisition/split_receipts/{parent}.json",
                {"parent_query_id": parent, "split_receipt_id": "f" * 64},
            )
            payload["split_receipts"] = [
                {
                    "parent_query_id": parent,
                    "path": fake_split.relative_to(root).as_posix(),
                    "sha256": _sha256(fake_split),
                    "split_receipt_id": "f" * 64,
                }
            ]
            paths = self.registry._paths(root, run_spec)
            with (
                patch.object(
                    v52_query_plan,
                    "load_split_receipt",
                    return_value=None,
                ) as load_split,
                self.assertRaisesRegex(ValueError, "split|child|receipt"),
            ):
                with self.registry._registry_lock(
                    paths, create=False, exclusive=False
                ) as lock_binding:
                    self.registry._checkpoint_artifacts_and_population(
                        paths,
                        run_spec,
                        payload,
                        lock_binding,
                    )
            load_split.assert_called_once_with(
                root,
                parent,
                evidence_root=(
                    root / run_spec.raw["namespaces"]["raw"]
                ),
                read_only_progress=False,
            )

    def test_checkpoint_publish_evidence_race_leaves_no_poisoned_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            stage_evidence = self.write_artifact(
                root, "audit/checkpoint-stage-evidence.json", {"version": 1}
            )

            def mutate_evidence() -> None:
                stage_evidence.write_bytes(
                    canonical_json_bytes({"version": 2}) + b"\n"
                )

            with (
                patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=mutate_evidence,
                ),
                self.assertRaisesRegex(ValueError, "artifact|evidence|snapshot"),
            ):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                    stage_evidence_receipts=(stage_evidence,),
                )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(list(checkpoint_dir.iterdir()), [])
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec)
            )

    def test_checkpoint_post_link_membership_failure_cleans_owned_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            unknown = checkpoint_dir / "unknown.json"

            def add_unknown_after_final_read() -> None:
                unknown.write_bytes(b"{}\n")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=add_unknown_after_final_read,
                ),
                self.assertRaisesRegex(
                    ValueError, "checkpoint|membership|gap|fork|unknown"
                ),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )
            self.assertEqual(list(checkpoint_dir.iterdir()), [unknown])
            unknown.unlink()
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec)
            )

    def test_checkpoint_membership_cleanup_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            unknown = checkpoint_dir / "unknown.json"

            def add_unknown_after_final_read() -> None:
                unknown.write_bytes(b"{}\n")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=add_unknown_after_final_read,
                ),
                patch.object(
                    self.registry,
                    "_cleanup_created_receipt",
                    side_effect=OSError("injected cleanup failure"),
                ),
                self.assertRaisesRegex(ValueError, "cleanup ownership verification"),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )

    def test_sources_ready_requires_terminal_checkpoint_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(
                root, terminal_checkpoint=False
            )
            with self.assertRaisesRegex(ValueError, "checkpoint|artifact evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )

    def test_non_static_checkpoint_stages_require_their_own_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            for stage in (
                "PARTICIPANT_DERIVED",
                "REMAINING",
            ):
                with self.subTest(stage=stage), self.assertRaisesRegex(
                    ValueError,
                    rf"\Acheckpoint {stage} stage lacks its batch\Z",
                ):
                    self.registry.publish_acquisition_checkpoint(
                        root,
                        run_spec,
                        expected_prior_checkpoint_sha256=None,
                        stage=stage,
                        query_batch_ids=(batch.batch_id,),
                        completed_query_ids=(),
                        pending_query_ids=query_ids,
                        quarantined_query_ids=(),
                        split_query_ids=(),
                    )

    def test_timezone_checkpoint_is_terminal_no_batch_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            fixture, stage_evidence, reference = self.terminal_timezone_fixture(
                root, run_spec
            )
            checkpoint = self.publish_timezone_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=reference.receipt_sha256,
                stage_evidence=stage_evidence,
            )
            self.assertEqual(checkpoint.stage, "TIMEZONE_REVIEW")
            self.assertEqual(
                checkpoint.query_batch_ids,
                (fixture[0].batch_id, fixture[1].batch_id),
            )
            self.assertFalse(checkpoint.pending_query_ids)
            self.assertFalse(checkpoint.quarantined_query_ids)
            self.assertFalse(checkpoint.split_query_ids)
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                checkpoint,
            )
            self.assertEqual(
                self.publish_timezone_checkpoint(
                    root,
                    run_spec,
                    fixture,
                    expected_prior=checkpoint.receipt_sha256,
                    stage_evidence=stage_evidence,
                ),
                checkpoint,
            )

    def test_timezone_checkpoint_rejects_nonterminal_and_extra_evidence(self):
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )

        for attack in ("nonterminal", "extra-evidence", "new-batch"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                fixture = self.reference_checkpoint_fixture(root, run_spec)
                *_, static_checkpoint, _ = fixture
                query_batch_ids = None
                if attack == "nonterminal":
                    reference = self.publish_reference_checkpoint(
                        root,
                        run_spec,
                        fixture,
                        expected_prior=static_checkpoint.receipt_sha256,
                    )
                stage_evidence = self.complete_reference_and_timezone_gate(
                    root, fixture
                )
                if attack != "nonterminal":
                    reference = self.publish_reference_checkpoint(
                        root,
                        run_spec,
                        fixture,
                        expected_prior=static_checkpoint.receipt_sha256,
                        completed_reference_ids=fixture[3],
                    )
                if attack == "extra-evidence":
                    extra = self.write_artifact(
                        root,
                        "audit/v5_2_qd/historical/acquisition/timezone-extra.json",
                        {"extra": True},
                    )
                    stage_evidence = (*stage_evidence, extra)
                elif attack == "new-batch":
                    timezone_batch = _make_batch(
                        "TIMEZONE_REVIEW",
                        None,
                        (build_statcast_root_batch(root).queries[0],),
                    )
                    publish_query_batch(root, timezone_batch)
                    query_batch_ids = (
                        fixture[0].batch_id,
                        fixture[1].batch_id,
                        timezone_batch.batch_id,
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "TIMEZONE_REVIEW|terminal|pending|stage evidence|authority|fully verified",
                ):
                    self.publish_timezone_checkpoint(
                        root,
                        run_spec,
                        fixture,
                        expected_prior=reference.receipt_sha256,
                        stage_evidence=stage_evidence,
                        query_batch_ids=query_batch_ids,
                    )

    def test_timezone_checkpoint_historical_load_rechecks_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            fixture, stage_evidence, reference = self.terminal_timezone_fixture(
                root, run_spec
            )
            self.publish_timezone_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=reference.receipt_sha256,
                stage_evidence=stage_evidence,
            )
            candidate = root / TIMEZONE_STAGE_EVIDENCE[3]
            candidate.write_bytes(candidate.read_bytes() + b" ")
            with self.assertRaisesRegex(
                ValueError, "timezone|candidate|gate|checkpoint|evidence"
            ):
                self.registry.load_latest_acquisition_checkpoint(root, run_spec)

    def test_timezone_checkpoint_post_link_gate_race_cleans_new_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            fixture, stage_evidence, reference = self.terminal_timezone_fixture(
                root, run_spec
            )
            static_checkpoint = fixture[5]
            candidate = root / TIMEZONE_STAGE_EVIDENCE[3]
            original = candidate.read_bytes()

            def mutate_candidate() -> None:
                candidate.write_bytes(original + b" ")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=mutate_candidate,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "timezone|candidate|gate|artifact|evidence|snapshot",
                ),
            ):
                self.publish_timezone_checkpoint(
                    root,
                    run_spec,
                    fixture,
                    expected_prior=reference.receipt_sha256,
                    stage_evidence=stage_evidence,
                )
            candidate.write_bytes(original)
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(
                sorted(path.name for path in checkpoint_dir.iterdir()),
                [
                    f"00000000-{static_checkpoint.receipt_sha256}.json",
                    f"00000001-{reference.receipt_sha256}.json",
                ],
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                reference,
            )

    def test_terminal_timezone_opens_partial_participant_with_exact_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            participant_fixture = self.participant_checkpoint_fixture(
                root, run_spec
            )
            (
                fixture,
                timezone,
                inventory,
                participant_batch,
                participant_ids,
                stage_evidence,
            ) = participant_fixture
            static_batch, reference_batch, static_ids, reference_ids, *_ = fixture

            checkpoint = self.publish_participant_checkpoint(
                root,
                run_spec,
                participant_fixture,
                expected_prior=timezone.receipt_sha256,
            )
            self.assertEqual(timezone.stage, "TIMEZONE_REVIEW")
            self.assertEqual(checkpoint.stage, "PARTICIPANT_DERIVED")
            self.assertEqual(
                checkpoint.query_batch_ids,
                (
                    static_batch.batch_id,
                    reference_batch.batch_id,
                    participant_batch.batch_id,
                ),
            )
            self.assertEqual(
                checkpoint.stage_evidence_receipt_sha256s,
                tuple(_sha256(path) for path in stage_evidence),
            )
            self.assertEqual(len(checkpoint.query_batch_ids), 3)
            self.assertEqual(len(checkpoint.stage_evidence_receipt_sha256s), 7)
            self.assertEqual(
                checkpoint.completed_query_ids,
                tuple(sorted((*static_ids, *reference_ids))),
            )
            self.assertEqual(checkpoint.pending_query_ids, participant_ids)
            self.assertFalse(checkpoint.quarantined_query_ids)
            self.assertFalse(checkpoint.split_query_ids)
            self.assertEqual(
                participant_batch.derived_from_sha256,
                inventory.sha256,
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                checkpoint,
            )

    def test_partial_participant_advances_terminal_and_retries_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            participant_fixture = self.participant_checkpoint_fixture(
                root, run_spec
            )
            _, timezone, _, participant_batch, participant_ids, _ = (
                participant_fixture
            )
            partial = self.publish_participant_checkpoint(
                root,
                run_spec,
                participant_fixture,
                expected_prior=timezone.receipt_sha256,
            )
            self.complete_participant_queries(root, run_spec, participant_batch)
            terminal = self.publish_participant_checkpoint(
                root,
                run_spec,
                participant_fixture,
                expected_prior=partial.receipt_sha256,
                completed_participant_ids=participant_ids,
            )
            self.assertFalse(terminal.pending_query_ids)
            self.assertFalse(terminal.quarantined_query_ids)
            self.assertFalse(terminal.split_query_ids)
            self.assertEqual(
                self.publish_participant_checkpoint(
                    root,
                    run_spec,
                    participant_fixture,
                    expected_prior=terminal.receipt_sha256,
                    completed_participant_ids=participant_ids,
                ),
                terminal,
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                terminal,
            )

    def test_participant_checkpoint_rejects_nonterminal_prior_wrong_evidence_and_split(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            participant_fixture = self.participant_checkpoint_fixture(
                root,
                run_spec,
                terminal_timezone_checkpoint=False,
            )
            (
                fixture,
                reference,
                _,
                participant_batch,
                participant_ids,
                participant_evidence,
            ) = participant_fixture
            with self.assertRaisesRegex(
                ValueError,
                "PARTICIPANT_DERIVED|stage|TIMEZONE|prior|fully verified|authority",
            ):
                self.publish_participant_checkpoint(
                    root,
                    run_spec,
                    participant_fixture,
                    expected_prior=reference.receipt_sha256,
                )

            timezone = self.publish_timezone_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=reference.receipt_sha256,
                stage_evidence=tuple(root / path for path in TIMEZONE_STAGE_EVIDENCE),
            )
            participant_fixture = (
                fixture,
                timezone,
                *participant_fixture[2:],
            )
            static_batch, reference_batch, *_ = fixture
            wrong_evidence = tuple(
                path
                for path in participant_evidence
                if path.name != "game_evidence_inventory.json"
            )
            for attack, kwargs in (
                (
                    "wrong-batch",
                    {
                        "query_batch_ids": (
                            static_batch.batch_id,
                            participant_batch.batch_id,
                            reference_batch.batch_id,
                        )
                    },
                ),
                ("wrong-evidence", {"stage_evidence": wrong_evidence}),
                (
                    "split",
                    {"split_participant_ids": (participant_ids[0],)},
                ),
                (
                    "wrong-status",
                    {"completed_participant_ids": (participant_ids[0],)},
                ),
            ):
                with (
                    self.subTest(attack=attack),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "PARTICIPANT_DERIVED|batch|order|evidence|split|status|authority|checkpoint",
                    ),
                ):
                    self.publish_participant_checkpoint(
                        root,
                        run_spec,
                        participant_fixture,
                        expected_prior=timezone.receipt_sha256,
                        **kwargs,
                    )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                timezone,
            )

    def test_participant_historical_inventory_or_batch_mutation_is_rejected(
        self,
    ) -> None:
        for authority in ("inventory", "batch"):
            with (
                self.subTest(authority=authority),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, run_spec = self.make_repo(Path(directory))
                participant_fixture = self.participant_checkpoint_fixture(
                    root, run_spec
                )
                _, timezone, inventory, participant_batch, *_ = (
                    participant_fixture
                )
                checkpoint = self.publish_participant_checkpoint(
                    root,
                    run_spec,
                    participant_fixture,
                    expected_prior=timezone.receipt_sha256,
                )
                if authority == "inventory":
                    payload = json.loads(inventory.path.read_bytes())
                    payload["counts"]["game_count"] += 1
                    inventory.path.write_bytes(
                        canonical_json_bytes(payload) + b"\n"
                    )
                else:
                    batch_path = (
                        root
                        / "audit/v5_2_qd/historical/acquisition/query_batches"
                        / f"{participant_batch.batch_id}.json"
                    )
                    batch_path.write_bytes(batch_path.read_bytes() + b" ")
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "PARTICIPANT_DERIVED|inventory|batch|hash|checkpoint|authority",
                ):
                    self.registry.load_latest_acquisition_checkpoint(root, run_spec)
                checkpoint_dir = (
                    root
                    / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                )
                self.assertTrue(
                    any(
                        checkpoint.receipt_sha256 in path.name
                        for path in checkpoint_dir.iterdir()
                    )
                )

    def test_participant_post_link_inventory_race_cleans_owned_new_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            participant_fixture = self.participant_checkpoint_fixture(
                root, run_spec
            )
            _, timezone, inventory, *_ = participant_fixture
            original = inventory.path.read_bytes()
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            before = sorted(path.name for path in checkpoint_dir.iterdir())

            def mutate_inventory() -> None:
                inventory.path.write_bytes(original + b" ")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=mutate_inventory,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "PARTICIPANT_DERIVED|inventory|artifact|evidence|snapshot|authority",
                ),
            ):
                self.publish_participant_checkpoint(
                    root,
                    run_spec,
                    participant_fixture,
                    expected_prior=timezone.receipt_sha256,
                )
            inventory.path.write_bytes(original)
            self.assertEqual(
                sorted(path.name for path in checkpoint_dir.iterdir()),
                before,
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                timezone,
            )

    def test_reference_checkpoint_opens_with_exact_fixed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            fixture = self.reference_checkpoint_fixture(root, run_spec)
            static_batch, reference_batch, static_ids, reference_ids, inventory, static_checkpoint, _ = fixture
            checkpoint = self.publish_reference_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=static_checkpoint.receipt_sha256,
            )
            self.assertEqual(checkpoint.stage, "REFERENCE_INPUTS")
            self.assertEqual(
                checkpoint.query_batch_ids,
                (static_batch.batch_id, reference_batch.batch_id),
            )
            self.assertEqual(checkpoint.completed_query_ids, static_ids)
            self.assertEqual(checkpoint.pending_query_ids, reference_ids)
            self.assertEqual(
                checkpoint.stage_evidence_receipt_sha256s,
                (inventory.sha256,),
            )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                checkpoint,
            )

    def test_reference_checkpoint_rejects_noncanonical_batches_and_stage_evidence(self):
        from mlb_backtest.v52_query_plan import build_statcast_root_batch, publish_query_batch

        for attack in ("omitted", "extra", "missing-evidence", "extra-evidence"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                fixture = self.reference_checkpoint_fixture(root, run_spec)
                static_batch, reference_batch, static_ids, reference_ids, inventory, static_checkpoint, _ = fixture
                batch_ids = (static_batch.batch_id, reference_batch.batch_id)
                stage_evidence = (inventory.path,)
                if attack == "omitted":
                    batch_ids = (static_batch.batch_id,)
                elif attack == "extra":
                    extra = build_statcast_root_batch(root)
                    publish_query_batch(root, extra)
                    batch_ids = (*batch_ids, extra.batch_id)
                elif attack == "missing-evidence":
                    stage_evidence = ()
                else:
                    extra_evidence = self.write_artifact(
                        root,
                        "audit/v5_2_qd/historical/acquisition/extra.json",
                        {"extra": True},
                    )
                    stage_evidence = (inventory.path, extra_evidence)
                with self.assertRaisesRegex(
                    ValueError,
                    "REFERENCE_INPUTS|reference|inventory|batch|stage evidence|order",
                ):
                    self.registry.publish_acquisition_checkpoint(
                        root,
                        run_spec,
                        expected_prior_checkpoint_sha256=static_checkpoint.receipt_sha256,
                        stage="REFERENCE_INPUTS",
                        query_batch_ids=batch_ids,
                        completed_query_ids=static_ids,
                        pending_query_ids=reference_ids,
                        quarantined_query_ids=(),
                        split_query_ids=(),
                        stage_evidence_receipts=stage_evidence,
                    )

    def test_reference_checkpoint_authority_mutation_cleans_new_receipt_and_load_fails(self):
        for operation in ("publish-pre", "publish-post", "load"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                fixture = self.reference_checkpoint_fixture(root, run_spec)
                *_, inventory, static_checkpoint, _ = fixture
                checkpoint_dir = root / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"

                def mutate_inventory() -> None:
                    payload = json.loads(inventory.path.read_bytes())
                    payload["counts"]["game_count"] += 1
                    inventory.path.write_bytes(canonical_json_bytes(payload) + b"\n")

                if operation == "load":
                    checkpoint = self.publish_reference_checkpoint(
                        root,
                        run_spec,
                        fixture,
                        expected_prior=static_checkpoint.receipt_sha256,
                    )
                    mutate_inventory()
                    with self.assertRaisesRegex(
                        (ValueError, RuntimeError), "inventory|reference|checkpoint"
                    ):
                        self.registry.load_latest_acquisition_checkpoint(root, run_spec)
                    self.assertTrue(
                        any(checkpoint.receipt_sha256 in path.name for path in checkpoint_dir.iterdir())
                    )
                    continue
                hook = (
                    "_before_registry_publish"
                    if operation == "publish-pre"
                    else "_after_registry_publish"
                )
                with (
                    patch.object(self.registry, hook, side_effect=mutate_inventory),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "inventory|artifact|evidence|checkpoint|reference",
                    ),
                ):
                    self.publish_reference_checkpoint(
                        root,
                        run_spec,
                        fixture,
                        expected_prior=static_checkpoint.receipt_sha256,
                    )
                self.assertEqual(
                    sorted(path.name for path in checkpoint_dir.iterdir()),
                    [
                        f"00000000-{static_checkpoint.receipt_sha256}.json"
                    ],
                )

    def test_reference_terminal_progress_is_repeatable_stop_after_authority(self):
        from mlb_backtest.v52_evidence import AttemptCapture

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            fixture = self.reference_checkpoint_fixture(root, run_spec)
            _, reference_batch, _, reference_ids, _, static_checkpoint, store = fixture
            pending = self.publish_reference_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=static_checkpoint.receipt_sha256,
            )
            for query in reference_batch.queries:
                raw = b"{}"
                capture = AttemptCapture(
                    started_at_utc="2026-07-14T00:00:00.000000Z",
                    ended_at_utc="2026-07-14T00:00:01.000000Z",
                    final_url=query.endpoint,
                    http_status=200,
                    response_headers=(
                        ("content-type", "application/json"),
                        ("content-length", str(len(raw))),
                    ),
                    body=raw,
                    error_classification=None,
                )
                with store.acquire_query(query) as lease:
                    store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        capture,
                        {},
                    )
            terminal = self.publish_reference_checkpoint(
                root,
                run_spec,
                fixture,
                expected_prior=pending.receipt_sha256,
                completed_reference_ids=reference_ids,
            )
            self.assertFalse(terminal.pending_query_ids)
            self.assertFalse(terminal.quarantined_query_ids)
            self.assertFalse(terminal.split_query_ids)
            self.assertEqual(
                self.publish_reference_checkpoint(
                    root,
                    run_spec,
                    fixture,
                    expected_prior=terminal.receipt_sha256,
                    completed_reference_ids=reference_ids,
                ),
                terminal,
            )

    def test_reference_stored_checkpoint_rejects_forged_evidence_labels(self):
        from mlb_backtest.v52_evidence import AttemptCapture

        for attack in ("success-path", "quarantine-path", "wrong-query"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                fixture = self.reference_checkpoint_fixture(root, run_spec)
                _, reference_batch, _, reference_ids, inventory, static_checkpoint, store = fixture
                completed_reference_ids: tuple[str, ...] = ()
                if attack == "wrong-query":
                    query = reference_batch.queries[0]
                    raw = b"{}"
                    capture = AttemptCapture(
                        started_at_utc="2026-07-14T00:00:00.000000Z",
                        ended_at_utc="2026-07-14T00:00:01.000000Z",
                        final_url=query.endpoint,
                        http_status=200,
                        response_headers=(
                            ("content-type", "application/json"),
                            ("content-length", str(len(raw))),
                        ),
                        body=raw,
                        error_classification=None,
                    )
                    with store.acquire_query(query) as lease:
                        store.publish_fetched(
                            query,
                            store.next_attempt_number(lease),
                            capture,
                            {},
                        )
                    from mlb_backtest.v52_evidence import logical_query_id

                    completed_reference_ids = (logical_query_id(query),)
                checkpoint = self.publish_reference_checkpoint(
                    root,
                    run_spec,
                    fixture,
                    expected_prior=static_checkpoint.receipt_sha256,
                    completed_reference_ids=completed_reference_ids,
                )
                checkpoint_dir = root / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                receipt = next(
                    path
                    for path in checkpoint_dir.iterdir()
                    if checkpoint.receipt_sha256 in path.name
                )
                payload = json.loads(receipt.read_bytes())
                target = next(
                    query_id
                    for query_id in reference_ids
                    if query_id not in completed_reference_ids
                )
                payload["pending_query_ids"].remove(target)
                if attack == "quarantine-path":
                    payload["quarantined_query_ids"].append(target)
                    kind = "QUARANTINE"
                    forged_binding = {
                        "path": inventory.path.relative_to(root).as_posix(),
                        "query_id": target,
                        "receipt_kind": kind,
                        "sha256": inventory.sha256,
                    }
                elif attack == "wrong-query":
                    payload["completed_query_ids"].append(target)
                    kind = "SUCCESS"
                    source = next(
                        item
                        for item in payload["query_evidence_receipts"]
                        if item["receipt_kind"] == "SUCCESS"
                        and item["query_id"] in completed_reference_ids
                    )
                    forged_binding = {**source, "query_id": target}
                else:
                    payload["completed_query_ids"].append(target)
                    kind = "SUCCESS"
                    forged_binding = {
                        "path": inventory.path.relative_to(root).as_posix(),
                        "query_id": target,
                        "receipt_kind": kind,
                        "sha256": inventory.sha256,
                    }
                payload["completed_query_ids"].sort()
                payload["quarantined_query_ids"].sort()
                payload["query_evidence_receipts"].append(forged_binding)
                payload["query_evidence_receipts"].sort(
                    key=lambda item: (
                        item["query_id"],
                        item["receipt_kind"],
                        item["path"],
                    )
                )
                self.replace_receipt_payload(receipt, payload)
                with self.assertRaisesRegex(
                    ValueError, "status|evidence|query|path|checkpoint"
                ):
                    self.registry.load_latest_acquisition_checkpoint(root, run_spec)

    def test_reference_stored_status_supports_current_quarantine_and_recovery(self):
        from mlb_backtest.v52_evidence import AttemptCapture, logical_query_id

        for status in ("QUARANTINED", "SUCCESS"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                fixture = self.reference_checkpoint_fixture(root, run_spec)
                _, reference_batch, _, _, _, static_checkpoint, store = fixture
                query = reference_batch.queries[0]
                query_id = logical_query_id(query)
                quarantined_raw = b"invalid-but-owned"
                quarantined_capture = AttemptCapture(
                    started_at_utc="2026-07-14T00:00:00.000000Z",
                    ended_at_utc="2026-07-14T00:00:01.000000Z",
                    final_url=query.endpoint,
                    http_status=200,
                    response_headers=(
                        ("content-type", "application/json"),
                        ("content-length", str(len(quarantined_raw))),
                    ),
                    body=quarantined_raw,
                    error_classification=None,
                )
                with store.acquire_query(query) as lease:
                    store.quarantine_attempt(
                        query,
                        store.next_attempt_number(lease),
                        quarantined_capture,
                        error_classification="SEMANTIC_INVALID",
                    )
                completed: tuple[str, ...] = ()
                quarantined = (query_id,)
                if status == "SUCCESS":
                    raw = b"{}"
                    success_capture = AttemptCapture(
                        started_at_utc="2026-07-14T00:00:02.000000Z",
                        ended_at_utc="2026-07-14T00:00:03.000000Z",
                        final_url=query.endpoint,
                        http_status=200,
                        response_headers=(
                            ("content-type", "application/json"),
                            ("content-length", str(len(raw))),
                        ),
                        body=raw,
                        error_classification=None,
                    )
                    with store.acquire_query(query) as lease:
                        store.publish_fetched(
                            query,
                            store.next_attempt_number(lease),
                            success_capture,
                            {},
                        )
                    completed = (query_id,)
                    quarantined = ()
                checkpoint = self.publish_reference_checkpoint(
                    root,
                    run_spec,
                    fixture,
                    expected_prior=static_checkpoint.receipt_sha256,
                    completed_reference_ids=completed,
                    quarantined_reference_ids=quarantined,
                )
                self.assertEqual(
                    query_id in checkpoint.completed_query_ids,
                    status == "SUCCESS",
                )
                self.assertEqual(
                    query_id in checkpoint.quarantined_query_ids,
                    status == "QUARANTINED",
                )
                self.assertEqual(
                    self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                    checkpoint,
                )

    def test_static_checkpoint_replays_canonical_builder_authority(self) -> None:
        from dataclasses import replace

        from mlb_backtest import v52_query_plan

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            different = replace(batch, batch_id="f" * 64)
            with (
                patch.object(
                    v52_query_plan,
                    "build_static_batch",
                    return_value=different,
                ),
                self.assertRaisesRegex(ValueError, "STATIC_SCHEDULES|authority"),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )

    def test_sources_ready_publication_rechecks_full_checkpoint_membership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(
                root, terminal_checkpoint=False
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            first_contents = canonical_json_bytes({"fixture": "interior"}) + b"\n"
            head_contents = canonical_json_bytes({"fixture": "head"}) + b"\n"
            first_sha = hashlib.sha256(first_contents).hexdigest()
            head_sha = hashlib.sha256(head_contents).hexdigest()
            names = (
                f"00000000-{first_sha}.json",
                f"00000001-{head_sha}.json",
            )
            first_path = checkpoint_dir / names[0]
            first_path.write_bytes(first_contents)
            (checkpoint_dir / names[1]).write_bytes(head_contents)

            def checkpoint(sequence: int, digest: str):
                return self.registry.AcquisitionCheckpoint(
                    sequence=sequence,
                    receipt_sha256=digest,
                    prior_checkpoint_sha256=first_sha if sequence else None,
                    stage="REMAINING",
                    recorded_at_utc="2026-07-14T00:00:00.000000Z",
                    git_head="a" * 40,
                    execution_receipt_sha256="b" * 64,
                    query_batch_ids=(),
                    query_batch_sha256s=(),
                    query_evidence_receipt_sha256s=(),
                    stage_evidence_receipt_sha256s=(),
                    split_receipt_sha256s=(),
                    completed_query_ids=(),
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                    stage_evidence_receipt_paths=(),
                )

            checkpoints = (checkpoint(0, first_sha), checkpoint(1, head_sha))
            payloads = (
                {"pending_query_ids": [], "quarantined_query_ids": []},
                {"pending_query_ids": [], "quarantined_query_ids": []},
            )

            def load_chain(*args: object, **kwargs: object):
                del args, kwargs
                if tuple(sorted(path.name for path in checkpoint_dir.iterdir())) != names:
                    raise self.registry._ReceiptMembershipError(
                        "checkpoint membership changed"
                    )
                return checkpoints, payloads, names

            def delete_interior() -> None:
                first_path.unlink(missing_ok=True)

            with (
                patch.object(
                    self.registry,
                    "_load_checkpoints_locked",
                    side_effect=load_chain,
                ),
                patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=delete_interior,
                ),
                self.assertRaisesRegex(ValueError, "checkpoint|membership"),
            ):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertFalse(
                any(
                    path.name.startswith("000002-SOURCES_READY-")
                    for path in (root / RECEIPTS_RELATIVE).iterdir()
                )
            )

    def test_sources_ready_rejects_consistent_review_ids_unrelated_to_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            for receipt in evidence:
                payload = json.loads(receipt.read_bytes())
                payload["reviewed_query_ids"] = ["unrelated-query"]
                receipt.write_bytes(canonical_json_bytes(payload) + b"\n")

            with self.assertRaisesRegex(ValueError, "artifact|evidence|query"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_sources_ready_allows_audited_review_subset_with_full_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            review_marker, replay_one, replay_two = evidence
            review = self.source_receipt_from_marker(root, review_marker)
            review_payload = json.loads(review.read_bytes())
            review_payload["reviewed_query_ids"] = ["1" * 64]
            selection = root / review_payload["query_ids_document"]["path"]
            selection.write_bytes(
                canonical_json_bytes({"query_ids": ["1" * 64]}) + b"\n"
            )
            review_payload["query_ids_document"]["sha256"] = _sha256(selection)
            review.write_bytes(canonical_json_bytes(review_payload) + b"\n")
            self.rebind_source_marker(review_marker, review)

            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            self.assertEqual(state.phase, "SOURCES_READY")
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), state
            )

    def test_sources_ready_rejects_invalid_review_subset_or_partial_replay(self) -> None:
        cases = ("empty", "outside", "duplicate", "unsorted", "partial_replay")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review_marker, replay_one_marker, replay_two_marker = evidence
                target_marker = (
                    replay_one_marker if case == "partial_replay" else review_marker
                )
                target = self.source_receipt_from_marker(root, target_marker)
                payload = json.loads(target.read_bytes())
                replacements = {
                    "empty": [],
                    "outside": ["outside-query"],
                    "duplicate": ["1" * 64, "1" * 64],
                    "unsorted": ["2" * 64, "1" * 64],
                    "partial_replay": ["1" * 64],
                }
                payload["reviewed_query_ids"] = replacements[case]
                target.write_bytes(canonical_json_bytes(payload) + b"\n")
                self.rebind_source_marker(target_marker, target)

                with self.assertRaisesRegex(ValueError, "artifact|evidence|query"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )

    def test_dynamic_phase_evidence_is_stable_through_publication(self) -> None:
        for artifact_kind in ("inventory", "review", "replay"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
                inventory, evidence = self.write_real_source_evidence(root)
                target = inventory if artifact_kind == "inventory" else evidence[0 if artifact_kind == "review" else 1]

                def mutate_evidence() -> None:
                    target.write_bytes(target.read_bytes() + b" ")

                with patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=mutate_evidence,
                ), self.assertRaisesRegex(ValueError, "evidence|identity|snapshot|changed"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 2)
                target.write_bytes(target.read_bytes()[:-1])
                self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_publication_rejects_receipt_parent_or_file_replacement_after_hook(self) -> None:
        for attack in ("parent", "file"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                displaced = receipts.with_name("phase_receipts-displaced")

                def replace_published_path() -> None:
                    published = next(path for path in receipts.iterdir() if path.name.startswith("000001-"))
                    if attack == "parent":
                        receipts.rename(displaced)
                        receipts.mkdir()
                    else:
                        replacement = published.with_suffix(".replacement")
                        replacement.write_bytes(published.read_bytes())
                        os.replace(replacement, published)

                with patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=replace_published_path,
                ), self.assertRaisesRegex(ValueError, "identity|receipt|publication"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="DESIGN_ACCEPTED",
                        next_phase="ACQUIRING",
                        inventory_path=root / PROTECTED_RELATIVE,
                        validation_receipts=(),
                    )
                if attack == "parent":
                    self.assertEqual(list(receipts.iterdir()), [])
                    self.assertEqual(len(list(displaced.iterdir())), 1)
                self.assertEqual(initial.phase, "DESIGN_ACCEPTED")

    def test_publication_final_rebind_rejects_disappeared_or_replaced_receipt(self) -> None:
        for attack in ("disappear", "replace"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE

                def attack_after_final_read() -> None:
                    published = next(
                        path
                        for path in receipts.iterdir()
                        if path.name.startswith("000001-")
                    )
                    if attack == "disappear":
                        published.unlink()
                    else:
                        replacement = published.with_suffix(".replacement")
                        replacement.write_bytes(b"tampered\n")
                        os.replace(replacement, published)

                hook = getattr(
                    self.registry, "_after_registry_receipt_read", None
                )
                self.assertIsNotNone(
                    hook, "final receipt read/rebind race hook is missing"
                )
                with patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=attack_after_final_read,
                ), self.assertRaisesRegex(
                    ValueError, "identity|receipt|publication|cleanup"
                ):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="DESIGN_ACCEPTED",
                        next_phase="ACQUIRING",
                        inventory_path=root / PROTECTED_RELATIVE,
                        validation_receipts=(),
                    )
                if attack == "disappear":
                    self.assertFalse(
                        any(
                            path.name.startswith("000001-")
                            for path in receipts.iterdir()
                        )
                    )
                    self.assertEqual(
                        self.registry.load_execution_registry(root, run_spec), initial
                    )
                else:
                    self.assertTrue(
                        any(
                            path.name.startswith("000001-")
                            for path in receipts.iterdir()
                        )
                    )
                    with self.assertRaises(ValueError):
                        self.registry.load_execution_registry(root, run_spec)

    def test_publication_accepts_equivalent_receipt_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipts = root / RECEIPTS_RELATIVE

            def replace_with_equivalent_inode() -> None:
                published = next(
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                )
                intended = published.read_bytes()
                original_inode = published.stat().st_ino
                replacement = published.with_suffix(".replacement")
                replacement.write_bytes(intended)
                self.assertEqual(replacement.stat().st_nlink, 1)
                self.assertNotEqual(replacement.stat().st_ino, original_inode)
                os.replace(replacement, published)

            with patch.object(
                self.registry,
                "_after_registry_receipt_read",
                side_effect=replace_with_equivalent_inode,
            ):
                advanced = self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertEqual(advanced.phase, "ACQUIRING")
            self.assertEqual(
                len(
                    [
                        path
                        for path in receipts.iterdir()
                        if path.name.startswith("000001-")
                    ]
                ),
                1,
            )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), advanced
            )

    def test_publication_rejects_final_same_sequence_receipt_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipts = root / RECEIPTS_RELATIVE
            fork_path: Path | None = None

            def add_valid_same_sequence_fork() -> None:
                nonlocal fork_path
                published = next(
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                )
                payload = json.loads(published.read_bytes())
                payload["recorded_at_utc"] = "2099-01-01T00:00:00.000000Z"
                contents = canonical_json_bytes(payload) + b"\n"
                digest = hashlib.sha256(contents).hexdigest()
                fork_path = receipts / f"000001-ACQUIRING-{digest}.json"
                fork_path.write_bytes(contents)

            with patch.object(
                self.registry,
                "_after_registry_receipt_read",
                side_effect=add_valid_same_sequence_fork,
            ), self.assertRaisesRegex(ValueError, "fork|receipt|membership|chain"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertIsNotNone(fork_path)
            self.assertTrue(fork_path.is_file())
            self.assertEqual(
                [
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                ],
                [fork_path],
                "the caller-created final is cleaned without deleting the fork",
            )

    def test_partial_write_or_fsync_failure_removes_created_receipt(self) -> None:
        for failure in ("partial_write", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                execution_fd, descriptors = self.registry._open_directory(
                    root,
                    Path("audit/v5_2_qd/historical/execution"),
                    create=False,
                )
                if failure == "partial_write":
                    original_write = os.write
                    calls = 0

                    def fail_write(descriptor: int, contents: object) -> int:
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            return original_write(descriptor, bytes(contents[:8]))
                        raise OSError("injected partial write failure")

                    context = patch.object(self.registry.os, "write", side_effect=fail_write)
                else:
                    original_fsync = os.fsync

                    def fail_receipt_fsync(descriptor: int) -> None:
                        metadata = os.fstat(descriptor)
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_size:
                            raise OSError("injected receipt fsync failure")
                        original_fsync(descriptor)

                    context = patch.object(self.registry.os, "fsync", side_effect=fail_receipt_fsync)
                try:
                    with context, self.assertRaisesRegex(ValueError, "published|publication|safely"):
                        self.registry._write_exclusive(
                            execution_fd,
                            Path("phase_receipts/injected.json"),
                            b"receipt contents\n",
                            "execution receipt",
                            lambda: None,
                        )
                finally:
                    self.registry._close_directories(descriptors)
                self.assertEqual(len(list(receipts.iterdir())), 1)
                self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_git_binding_cannot_change_between_evidence_validation_and_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            original_capture = self.registry._capture_git_snapshot

            def mutate_and_commit(repo: Path, evidence: object):
                (root / DESIGN_RELATIVE).write_bytes((root / DESIGN_RELATIVE).read_bytes() + b"changed\n")
                _git(root, "add", DESIGN_RELATIVE.as_posix())
                _git(root, "commit", "-qm", "racing design")
                return original_capture(repo, evidence)

            with patch.object(
                self.registry,
                "_capture_git_snapshot",
                side_effect=mutate_and_commit,
            ), self.assertRaisesRegex(ValueError, "binding|Git|snapshot|design"):
                self.initialize(root, run_spec)
            self.assertEqual(list((root / RECEIPTS_RELATIVE).iterdir()), [])

    def test_skip_worktree_cannot_hide_dirty_bound_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            relative = BOUND_EXECUTION_CODE[0]
            _git(root, "update-index", "--skip-worktree", relative.as_posix())
            (root / relative).write_bytes((root / relative).read_bytes() + b"dirty\n")
            self.assertEqual(
                _git(root, "status", "--porcelain=v1", "--", relative.as_posix()),
                "",
            )
            with self.assertRaisesRegex(ValueError, "dirty|Git|index|HEAD|worktree"):
                self.initialize(root, run_spec)

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_directory_child_stat_failures_do_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "child"
            child.mkdir()
            child_inode = child.stat().st_ino
            original_fstat = os.fstat
            before = len(os.listdir("/dev/fd"))

            def fail_child(descriptor: int):
                metadata = original_fstat(descriptor)
                if metadata.st_ino == child_inode:
                    raise OSError("injected child fstat failure")
                return metadata

            with patch.object(self.registry.os, "fstat", side_effect=fail_child):
                for _ in range(20):
                    with self.assertRaises(OSError):
                        self.registry._open_directory(
                            root, Path("child"), create=False
                        )
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_registry_lock_flock_errors_close_every_descriptor(self) -> None:
        for failure in ("acquire", "release"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                paths = self.registry._paths(root, run_spec)
                original_flock = self.registry.fcntl.flock
                before = len(os.listdir("/dev/fd"))

                def fail_flock(descriptor: int, operation: int) -> None:
                    if failure == "acquire" or operation == self.registry.fcntl.LOCK_UN:
                        raise OSError(f"injected {failure} flock failure")
                    original_flock(descriptor, operation)

                with patch.object(
                    self.registry.fcntl, "flock", side_effect=fail_flock
                ):
                    for _ in range(20):
                        with self.assertRaisesRegex(
                            OSError, f"injected {failure} flock failure"
                        ):
                            with self.registry._registry_lock(
                                paths, create=False, exclusive=True
                            ):
                                pass
                self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_load_rejects_forged_semantically_impossible_phase_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            receipt = next(path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000001-"))
            unrelated = self.write_artifact(root, "unrelated-inventory.json", {"rows": 1})
            payload = json.loads(receipt.read_bytes())
            payload["inventory"] = {
                "path": unrelated.relative_to(root).as_posix(),
                "sha256": _sha256(unrelated),
            }
            self.replace_receipt_payload(receipt, payload)
            with self.assertRaisesRegex(ValueError, "ACQUIRING|phase|inventory|evidence"):
                self.registry.load_execution_registry(root, run_spec)

    def test_load_rejects_forged_sources_ready_review_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            review_marker = evidence[0]
            review = self.source_receipt_from_marker(root, review_marker)
            review_payload = json.loads(review.read_bytes())
            review_payload["reviewed_query_ids"] = ["unrelated-query"]
            review.write_bytes(canonical_json_bytes(review_payload) + b"\n")
            self.rebind_source_marker(review_marker, review)
            receipt = next(path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000002-"))
            payload = json.loads(receipt.read_bytes())
            payload["validation_receipts"][0]["sha256"] = _sha256(review_marker)
            self.replace_receipt_payload(receipt, payload)
            with self.assertRaisesRegex(ValueError, "source|query|phase|evidence"):
                self.registry.load_execution_registry(root, run_spec)

    def test_acquiring_binds_actual_protected_inventory_without_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            receipt = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000001-")
            )
            payload = json.loads(receipt.read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": PROTECTED_RELATIVE.as_posix(),
                    "sha256": _sha256(root / PROTECTED_RELATIVE),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(state.phase, "ACQUIRING")

    def test_sources_ready_binds_real_inventory_review_and_two_clean_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            inventory, evidence = self.write_real_source_evidence(root)
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            receipt = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000002-")
            )
            payload = json.loads(receipt.read_bytes())
            self.assertEqual(state.phase, "SOURCES_READY")
            self.assertEqual(
                payload["inventory"]["sha256"], _sha256(inventory)
            )
            self.assertEqual(
                [item["sha256"] for item in payload["validation_receipts"]],
                [_sha256(path) for path in evidence],
            )

    def test_rejects_inventory_and_validation_through_external_symlink_alias(self) -> None:
        for artifact_kind in ("inventory", "validation"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root, run_spec = self.make_repo(base)
                self.initialize(root, run_spec)
                alias = base / "repo_alias"
                alias.symlink_to(root, target_is_directory=True)
                if artifact_kind == "inventory":
                    initial = self.registry.load_execution_registry(root, run_spec)
                    expected_phase = "DESIGN_ACCEPTED"
                    next_phase = "ACQUIRING"
                    inventory = alias / PROTECTED_RELATIVE
                    evidence: tuple[Path, ...] = ()
                else:
                    initial = self.advance(
                        root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                    )
                    expected_phase = "ACQUIRING"
                    next_phase = "SOURCES_READY"
                    inventory, source_evidence = self.write_real_source_evidence(root)
                    evidence = (
                        alias / source_evidence[0].relative_to(root),
                        *source_evidence[1:],
                    )
                with self.assertRaisesRegex(ValueError, "root|symlink|path"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase=expected_phase,
                        next_phase=next_phase,
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )

    def advance(self, root: Path, run_spec: object, expected: str, phase: str, *, stem: str = "phase"):
        del stem
        if phase == "ACQUIRING":
            inventory = root / PROTECTED_RELATIVE
            evidence: tuple[Path, ...] = ()
        else:
            inventory, evidence = self.write_real_source_evidence(root)
        return self.registry.advance_execution_phase(
            root,
            run_spec,
            expected_phase=expected,
            next_phase=phase,
            inventory_path=inventory,
            validation_receipts=evidence,
        )

    def test_invalid_real_source_inventory_review_or_replay_quarantines(self) -> None:
        cases = (
            "inventory_status",
            "critical_errors",
            "network_attempts",
            "review_decision",
            "replay_status",
            "missing_replay",
            "inventory_hash",
            "snapshot_hashes",
            "reviewed_query_ids",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review_marker, replay_one_marker, replay_two_marker = evidence
                review = self.source_receipt_from_marker(root, review_marker)
                replay_one = self.source_receipt_from_marker(root, replay_one_marker)
                replay_two = self.source_receipt_from_marker(root, replay_two_marker)
                if case == "inventory_status":
                    payload = json.loads(inventory.read_bytes())
                    payload["status"] = "FAILED"
                    inventory.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "critical_errors":
                    payload = json.loads(review.read_bytes())
                    payload["critical_errors"] = [{"code": "FAILED"}]
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(review_marker, review)
                elif case == "network_attempts":
                    payload = json.loads(replay_one.read_bytes())
                    payload["network_attempts"] = 1
                    replay_one.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(replay_one_marker, replay_one)
                elif case == "review_decision":
                    payload = json.loads(review.read_bytes())
                    payload["decision"] = "REJECTED"
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(review_marker, review)
                elif case == "replay_status":
                    payload = json.loads(replay_two.read_bytes())
                    payload["status"] = "FAIL"
                    replay_two.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(replay_two_marker, replay_two)
                elif case == "missing_replay":
                    evidence = (review_marker, replay_one_marker)
                elif case == "inventory_hash":
                    payload = json.loads(review.read_bytes())
                    payload["inventory_sha256"] = "f" * 64
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(review_marker, review)
                elif case == "snapshot_hashes":
                    payload = json.loads(replay_one.read_bytes())
                    payload["snapshot_sha256s"] = ["c" * 64]
                    replay_one.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(replay_one_marker, replay_one)
                elif case == "reviewed_query_ids":
                    payload = json.loads(replay_two.read_bytes())
                    payload["reviewed_query_ids"] = ["different-query"]
                    replay_two.write_bytes(canonical_json_bytes(payload) + b"\n")
                    self.rebind_source_marker(replay_two_marker, replay_two)
                with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )
                quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
                self.assertEqual(len(list(quarantine.iterdir())), 1)

    def test_invalid_acquiring_inventory_quarantines_without_validation_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = self.write_artifact(
                root, "audit/v5_2_qd/historical/acquisition/wrong_inventory.json", {"wrong": True}
            )
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": inventory.relative_to(root).as_posix(),
                    "sha256": _sha256(inventory),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(
                payload["error_classification"], "DIRECT_INVENTORY_MISMATCH"
            )
            self.assertEqual(payload["attempted_phase"], "ACQUIRING")
            self.assertEqual(payload["current_phase"], "DESIGN_ACCEPTED")
            self.assertEqual(
                payload["current_receipt_sha256"], initial.receipt_sha256
            )
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            schema = json.loads(
                (root / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
            )
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)
            self.assertEqual(
                receipts[0].read_bytes(), canonical_json_bytes(payload) + b"\n"
            )

    def test_sources_ready_rejects_markerless_raw_source_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, raw_receipts = self.write_real_source_evidence(
                root, commit_markers=False
            )
            with self.assertRaisesRegex(ValueError, "source|evidence|commit"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=raw_receipts,
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_source_commit_marker_binding_and_underlying_uniqueness_are_enforced(self) -> None:
        for case in ("tampered_hash", "duplicate_underlying"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                _review_marker, replay_one_marker, replay_two_marker = evidence
                payload = json.loads(replay_two_marker.read_bytes())
                if case == "tampered_hash":
                    payload["source_receipt"]["sha256"] = "f" * 64
                else:
                    payload["source_receipt"] = json.loads(
                        replay_one_marker.read_bytes()
                    )["source_receipt"]
                replay_two_marker.write_bytes(canonical_json_bytes(payload) + b"\n")
                self._assert_sources_ready_rejected_and_quarantined(
                    root, run_spec, initial, inventory, evidence
                )

    def test_source_commit_marker_and_receipt_reject_aliased_file_types(self) -> None:
        for target_kind, attack in (
            ("marker", "symlink"),
            ("marker", "hardlink"),
            ("marker", "fifo"),
            ("receipt", "alias"),
            ("receipt", "symlink"),
            ("receipt", "hardlink"),
            ("receipt", "fifo"),
        ):
            with (
                self.subTest(target_kind=target_kind, attack=attack),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                marker = evidence[0]
                receipt = self.source_receipt_from_marker(root, marker)
                alias = marker.with_name(f"{target_kind}-{attack}.json")
                if attack == "alias":
                    payload = json.loads(marker.read_bytes())
                    payload["source_receipt"] = {
                        "path": marker.relative_to(root).as_posix(),
                        "sha256": _sha256(marker),
                    }
                    marker.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif attack == "symlink":
                    alias.symlink_to(marker if target_kind == "marker" else receipt)
                elif attack == "hardlink":
                    os.link(marker if target_kind == "marker" else receipt, alias)
                else:
                    os.mkfifo(alias)
                attacked = evidence
                if target_kind == "marker" and attack != "alias":
                    attacked = (alias, *evidence[1:])
                elif target_kind == "receipt" and attack != "alias":
                    payload = json.loads(marker.read_bytes())
                    payload["source_receipt"] = {
                        "path": alias.relative_to(root).as_posix(),
                        "sha256": (
                            _sha256(receipt) if attack != "fifo" else "a" * 64
                        ),
                    }
                    marker.write_bytes(canonical_json_bytes(payload) + b"\n")
                with self.assertRaisesRegex(
                    ValueError,
                    "artifact|source|evidence|alias|regular|unsafe|validation receipt|path",
                ):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=attacked,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )
    def test_sources_ready_missing_receipts_quarantines_with_inventory_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(
                root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
            )
            inventory, _evidence = self.write_real_source_evidence(root)
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": SOURCE_INVENTORY_RELATIVE.as_posix(),
                    "sha256": _sha256(inventory),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(
                payload["error_classification"],
                "SOURCE_GATE_EVIDENCE_INVALID",
            )

    def test_duplicate_source_review_quarantines_without_advancing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(
                root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
            )
            inventory, evidence = self.write_real_source_evidence(root)
            review, replay_one, replay_two = evidence
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(
                        review,
                        review,
                        replay_one,
                        replay_two,
                    ),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["error_classification"],
                "SOURCE_GATE_EVIDENCE_INVALID",
            )
            self.assertEqual(
                payload["validation_receipts"],
                [
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": _sha256(path),
                    }
                    for path in (review, review, replay_one, replay_two)
                ],
            )
            schema = json.loads(
                (root / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
            )
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)

    def _assert_sources_ready_rejected_and_quarantined(
        self,
        root: Path,
        run_spec: object,
        initial: object,
        inventory: Path,
        evidence: tuple[Path, ...],
    ) -> None:
        with self.assertRaisesRegex(ValueError, "schema|source|evidence|review|replay|binding"):
            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
        self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
        quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
        receipts = list(quarantine.iterdir())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_bytes())["error_classification"],
            "SOURCE_GATE_EVIDENCE_INVALID",
        )

    def test_source_receipt_schema_cannot_be_bypassed(self) -> None:
        cases = (
            "extra_replay_key",
            "missing_reviewer_role",
            "wrong_reviewer_role",
            "missing_execution_id",
            "malformed_execution_id",
            "non_v4_execution_id",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review_marker, replay_one_marker, _replay_two_marker = evidence
                target_marker = (
                    review_marker if "reviewer" in case else replay_one_marker
                )
                target = self.source_receipt_from_marker(root, target_marker)
                payload = json.loads(target.read_bytes())
                if case == "extra_replay_key":
                    payload["unexpected"] = True
                elif case == "missing_reviewer_role":
                    del payload["reviewer_role"]
                elif case == "wrong_reviewer_role":
                    payload["reviewer_role"] = "source-operator"
                elif case == "missing_execution_id":
                    del payload["execution_id"]
                elif case == "malformed_execution_id":
                    payload["execution_id"] = "not-a-uuid"
                else:
                    payload["execution_id"] = "11111111-1111-1111-8111-111111111111"
                target.write_bytes(canonical_json_bytes(payload) + b"\n")
                self.rebind_source_marker(target_marker, target)
                self._assert_sources_ready_rejected_and_quarantined(
                    root,
                    run_spec,
                    initial,
                    inventory,
                    evidence,
                )

    def test_duplicate_replay_execution_id_cannot_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            review_marker, replay_one_marker, replay_two_marker = evidence
            replay_one = self.source_receipt_from_marker(root, replay_one_marker)
            replay_two = self.source_receipt_from_marker(root, replay_two_marker)
            payload = json.loads(replay_two.read_bytes())
            payload["execution_id"] = json.loads(replay_one.read_bytes())["execution_id"]
            replay_two.write_bytes(canonical_json_bytes(payload) + b"\n")
            self.rebind_source_marker(replay_two_marker, replay_two)
            self._assert_sources_ready_rejected_and_quarantined(
                root,
                run_spec,
                initial,
                inventory,
                (review_marker, replay_one_marker, replay_two_marker),
            )

    def test_review_query_id_document_is_rebound(self) -> None:
        cases = ("path", "hash", "contents")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review_marker, _replay_one_marker, _replay_two_marker = evidence
                review = self.source_receipt_from_marker(root, review_marker)
                review_payload = json.loads(review.read_bytes())
                selection = root / review_payload["query_ids_document"]["path"]
                if case == "path":
                    replacement = self.write_artifact(
                        root,
                        "audit/v5_2_qd/historical/acquisition/source_replays/query_ids.json",
                        json.loads(selection.read_bytes()),
                    )
                    review_payload["query_ids_document"] = {
                        "path": replacement.relative_to(root).as_posix(),
                        "sha256": _sha256(replacement),
                    }
                    review.write_bytes(canonical_json_bytes(review_payload) + b"\n")
                    self.rebind_source_marker(review_marker, review)
                elif case == "hash":
                    selection.write_bytes(
                        canonical_json_bytes({"query_ids": ["2" * 64, "1" * 64]})
                        + b"\n"
                    )
                else:
                    selection.write_bytes(
                        canonical_json_bytes({"query_ids": ["2" * 64, "1" * 64]})
                        + b"\n"
                    )
                    review_payload["query_ids_document"]["sha256"] = _sha256(selection)
                    review.write_bytes(canonical_json_bytes(review_payload) + b"\n")
                    self.rebind_source_marker(review_marker, review)
                self._assert_sources_ready_rejected_and_quarantined(
                    root,
                    run_spec,
                    initial,
                    inventory,
                    evidence,
                )

    def test_source_schema_must_match_trusted_snapshot_through_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            _review_marker, replay_one_marker, _replay_two_marker = evidence
            replay_one = self.source_receipt_from_marker(root, replay_one_marker)
            replay_payload = json.loads(replay_one.read_bytes())
            replay_payload["unexpected"] = True
            replay_one.write_bytes(canonical_json_bytes(replay_payload) + b"\n")
            self.rebind_source_marker(replay_one_marker, replay_one)
            schema_path = root / "schemas/v5_2_clean_source_replay_receipt.schema.json"
            strict = schema_path.read_bytes()
            original = self.registry._validate_bound_schema

            def replace_schema_during_validation(*args: object, **kwargs: object) -> object:
                if "v5_2_clean_source_replay_receipt.schema.json" not in args:
                    return original(*args, **kwargs)
                permissive = json.loads(strict)
                permissive["additionalProperties"] = True
                schema_path.write_bytes(canonical_json_bytes(permissive) + b"\n")
                try:
                    return original(*args, **kwargs)
                finally:
                    schema_path.write_bytes(strict)

            with patch.object(
                self.registry,
                "_validate_bound_schema",
                side_effect=replace_schema_during_validation,
            ), self.assertRaisesRegex(ValueError, "schema|source|evidence|binding|Git"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            self.assertEqual(
                list((root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()),
                [],
            )

    def test_late_query_id_document_mutation_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            review_marker, _replay_one_marker, _replay_two_marker = evidence
            review = self.source_receipt_from_marker(root, review_marker)
            review_payload = json.loads(review.read_bytes())
            selection = root / review_payload["query_ids_document"]["path"]
            mutated = False

            def mutate_document_once() -> None:
                nonlocal mutated
                if not mutated:
                    mutated = True
                    selection.write_bytes(
                        canonical_json_bytes({"query_ids": ["2" * 64, "1" * 64]})
                        + b"\n"
                    )

            with patch.object(
                self.registry,
                "_before_registry_publish",
                side_effect=mutate_document_once,
            ):
                self._assert_sources_ready_rejected_and_quarantined(
                    root,
                    run_spec,
                    initial,
                    inventory,
                    evidence,
                )

    def test_late_tracked_schema_mutation_preserves_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            schema_path = root / "schemas/v5_2_clean_source_replay_receipt.schema.json"
            strict = schema_path.read_bytes()
            hook_calls = 0

            def replace_schema_once() -> None:
                nonlocal hook_calls
                hook_calls += 1
                schema_path.write_bytes(strict)

            with patch.object(
                self.registry,
                "_before_registry_publish",
                side_effect=replace_schema_once,
            ), self.assertRaisesRegex(ValueError, "bound Git file identity changed"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertEqual(hook_calls, 1)
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            self.assertEqual(
                list((root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()),
                [],
            )

    def test_late_source_evidence_schema_mutation_preserves_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            schema_path = root / "schemas/v5_2_clean_source_replay_receipt.schema.json"
            strict = schema_path.read_bytes()
            original_rebind = self.registry._verify_artifact_snapshots
            original_schema_validation = self.registry._validate_bound_schema
            mutated = False
            hook_calls = 0
            inventory_schema_calls = 0

            def mutate_after_dynamic_rebind(
                artifact_root: Path,
                snapshots: object,
                label: str,
            ) -> None:
                nonlocal mutated
                original_rebind(artifact_root, snapshots, label)
                if label == "dynamic phase evidence" and not mutated:
                    mutated = True
                    schema_path.write_bytes(strict)

            def count_hook() -> None:
                nonlocal hook_calls
                hook_calls += 1

            def preserve_old_inventory_contract(
                *args: object, **kwargs: object
            ):
                nonlocal inventory_schema_calls
                schema_name = args[2] if len(args) > 2 else kwargs["schema_name"]
                if schema_name == "v5_2_source_inventory.schema.json":
                    inventory_schema_calls += 1
                return original_schema_validation(*args, **kwargs)

            with patch.object(
                self.registry,
                "_verify_artifact_snapshots",
                side_effect=mutate_after_dynamic_rebind,
            ), patch.object(
                self.registry,
                "_validate_bound_schema",
                side_effect=preserve_old_inventory_contract,
            ), patch.object(
                self.registry,
                "_before_registry_publish",
                side_effect=count_hook,
            ), self.assertRaisesRegex(ValueError, "late source evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertTrue(mutated)
            self.assertEqual(hook_calls, 1)
            self.assertEqual(inventory_schema_calls, 0)
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            self.assertEqual(
                list((root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()),
                [],
            )

    def test_unresolvable_source_receipt_schema_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            schema_path = root / "schemas/v5_2_source_review_receipt.schema.json"
            strict = schema_path.read_bytes()
            original = self.registry._canonical_object

            def parse_broken_schema(contents: bytes, label: str) -> object:
                if label == "source receipt schema" and contents == strict:
                    broken = json.loads(strict)
                    broken["$ref"] = "#/$defs/missing"
                    return broken
                return original(contents, label)

            with patch.object(
                self.registry,
                "_canonical_object",
                side_effect=parse_broken_schema,
            ):
                self._assert_sources_ready_rejected_and_quarantined(
                    root,
                    run_spec,
                    initial,
                    inventory,
                    evidence,
                )

    def test_publication_rejects_replaced_execution_namespace(self) -> None:
        for operation in ("initialize", "advance", "quarantine"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = None
                if operation != "initialize":
                    initial = self.initialize(root, run_spec)
                execution = root / "audit/v5_2_qd/historical/execution"
                displaced = execution.with_name("execution-displaced")

                def replace_namespace() -> None:
                    execution.rename(displaced)
                    (execution / "phase_receipts").mkdir(parents=True)
                    (execution / "quarantine").mkdir()
                    (displaced / ".registry.lock").rename(
                        execution / ".registry.lock"
                    )

                hook = getattr(self.registry, "_before_registry_publish", None)
                self.assertIsNotNone(hook, "registry publish race hook is missing")
                with patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=replace_namespace,
                ), self.assertRaisesRegex(ValueError, "identity|namespace|lock"):
                    if operation == "initialize":
                        self.initialize(root, run_spec)
                    elif operation == "advance":
                        self.registry.advance_execution_phase(
                            root,
                            run_spec,
                            expected_phase="DESIGN_ACCEPTED",
                            next_phase="ACQUIRING",
                            inventory_path=root / PROTECTED_RELATIVE,
                            validation_receipts=(),
                        )
                    else:
                        failed = self.write_artifact(
                            root, "failed.json", {"status": "FAIL"}
                        )
                        self.registry.quarantine_execution_transition(
                            root,
                            run_spec,
                            attempted_phase="ACQUIRING",
                            validation_receipts=(failed,),
                        )

                self.assertEqual(list((execution / "phase_receipts").iterdir()), [])
                self.assertEqual(list((execution / "quarantine").iterdir()), [])
                (execution / ".registry.lock").rename(
                    displaced / ".registry.lock"
                )
                shutil.rmtree(execution)
                displaced.rename(execution)
                if initial is None:
                    self.assertEqual(
                        list((execution / "phase_receipts").iterdir()), []
                    )
                else:
                    self.assertEqual(
                        self.registry.load_execution_registry(root, run_spec),
                        initial,
                    )

    def test_publication_rolls_back_after_late_namespace_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            execution = root / "audit/v5_2_qd/historical/execution"
            displaced = execution.with_name("execution-displaced")

            def replace_namespace() -> None:
                execution.rename(displaced)
                (execution / "phase_receipts").mkdir(parents=True)
                (execution / "quarantine").mkdir()
                (displaced / ".registry.lock").rename(
                    execution / ".registry.lock"
                )

            hook = getattr(self.registry, "_after_registry_publish", None)
            self.assertIsNotNone(hook, "late registry publish hook is missing")
            with patch.object(
                self.registry,
                "_after_registry_publish",
                side_effect=replace_namespace,
            ), self.assertRaisesRegex(ValueError, "identity|namespace|lock"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertEqual(list((execution / "phase_receipts").iterdir()), [])
            self.assertEqual(
                len(list((displaced / "phase_receipts").iterdir())), 1
            )
            (execution / ".registry.lock").rename(displaced / ".registry.lock")
            shutil.rmtree(execution)
            displaced.rename(execution)
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )

    def test_rejects_git_head_identity_race_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            hook = getattr(self.registry, "_before_registry_publish", None)
            self.assertIsNotNone(hook, "registry publish race hook is missing")

            def race():
                _git(root, "commit", "--allow-empty", "-qm", "racing head")

            with patch.object(self.registry, "_before_registry_publish", side_effect=race), self.assertRaisesRegex(ValueError, "HEAD|identity|snapshot"):
                self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=root / PROTECTED_RELATIVE, validation_receipts=())
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)
            with self.assertRaisesRegex(ValueError, "Git HEAD|snapshot"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_lock_inode_replacement_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            original = self.registry._success_contents
            lock = root / "audit/v5_2_qd/historical/execution/.registry.lock"

            def replace_lock(payload, authority):
                contents = original(payload, authority)
                lock.unlink()
                lock.write_bytes(b"")
                return contents

            with patch.object(self.registry, "_success_contents", side_effect=replace_lock), self.assertRaisesRegex(ValueError, "lock.*identity|lock.*replaced"):
                self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=root / PROTECTED_RELATIVE, validation_receipts=())
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)

    def test_rejects_root_with_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real_parent = base / "real"
            real_parent.mkdir()
            root, run_spec = self.make_repo(real_parent)
            self.initialize(root, run_spec)
            alias = base / "alias"
            alias.symlink_to(real_parent, target_is_directory=True)
            aliased_root = alias / root.name
            with self.assertRaisesRegex(ValueError, "ancestor|symlink"):
                self.registry.load_execution_registry(aliased_root, run_spec)

    def test_initializes_design_accepted_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            first = self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            metadata = receipt.stat()
            before = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                receipt.read_bytes(),
            )
            second = self.initialize(root, run_spec)
            self.assertEqual(first, second)
            self.assertEqual(first.phase, "DESIGN_ACCEPTED")
            self.assertEqual(first.sequence, 0)
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)
            metadata = receipt.stat()
            self.assertEqual(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    receipt.read_bytes(),
                ),
                before,
            )
            self.assertTrue(receipt.read_bytes().endswith(b"\n"))
            self.assertFalse(receipt.read_bytes().endswith(b"\n\n"))
            self.assertEqual(
                receipt.name,
                f"000000-DESIGN_ACCEPTED-{first.receipt_sha256}.json",
            )

    def test_transition_requires_immediate_next_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})
            with self.assertRaisesRegex(ValueError, "immediate|phase"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
            self.assertEqual(list(quarantine.iterdir()), [])

    def test_transition_binds_prior_receipt_commit_specs_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=inventory,
                validation_receipts=(),
            )
            receipt_path = next(
                path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000001-")
            )
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["prior_receipt_sha256"], initial.receipt_sha256)
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            self.assertEqual(payload["run_spec"], {"path": RUN_SPEC_RELATIVE.as_posix(), "sha256": _sha256(root / RUN_SPEC_RELATIVE)})
            self.assertEqual(payload["inventory"], {"path": PROTECTED_RELATIVE.as_posix(), "sha256": _sha256(inventory)})
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(payload["implementation_plan"]["git_blob_id"], _git(root, "rev-parse", f"HEAD:{PLAN_RELATIVE}"))
            self.assertEqual(state.inventory_sha256, _sha256(inventory))

    def test_failed_validation_quarantines_without_advancing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            failed = self.write_artifact(root, "failed.json", {"status": "FAIL"})
            path = self.registry.quarantine_execution_transition(
                root,
                run_spec,
                attempted_phase="ACQUIRING",
                validation_receipts=(failed,),
            )
            self.assertIn("execution/quarantine", path.as_posix())
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["attempted_phase"], "ACQUIRING")
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            path.write_bytes(path.read_bytes()[:-2] + b"X\n")
            with self.assertRaisesRegex(ValueError, "quarantine.*hash"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_chain_gap_fork_tamper_symlink_hardlink_special_and_rollback(self) -> None:
        attacks = ("gap", "fork", "tamper", "symlink", "hardlink", "special", "rollback")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                first = next(receipts.iterdir())
                if attack == "rollback":
                    self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING", stem="one")
                    self.advance(root, run_spec, "ACQUIRING", "SOURCES_READY", stem="two")
                    next(path for path in receipts.iterdir() if path.name.startswith("000001-")).unlink()
                elif attack == "gap":
                    first.rename(receipts / first.name.replace("000000", "000001", 1))
                elif attack == "fork":
                    shutil.copy2(first, receipts / f"000000-DESIGN_ACCEPTED-{'0' * 64}.json")
                elif attack == "tamper":
                    first.write_bytes(first.read_bytes()[:-2] + b"X\n")
                else:
                    body = first.read_bytes()
                    first.unlink()
                    external = root / "external"
                    if attack == "symlink":
                        external.write_bytes(body)
                        first.symlink_to(external)
                    elif attack == "hardlink":
                        external.write_bytes(body)
                        os.link(external, first)
                    else:
                        os.mkfifo(first)
                        self.assertTrue(stat.S_ISFIFO(first.lstat().st_mode))
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    self.registry.load_execution_registry(root, run_spec)

    def test_concurrent_transition_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE

            barrier = threading.Barrier(2)

            def transition():
                barrier.wait()
                try:
                    state = self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=inventory, validation_receipts=())
                except ValueError as exc:
                    return "failure", str(exc)
                return "success", state

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: transition(), range(2)))
            self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["failure", "success"])
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 2)

    def test_identical_transition_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE
            arguments = {
                "expected_phase": "DESIGN_ACCEPTED",
                "next_phase": "ACQUIRING",
                "inventory_path": inventory,
                "validation_receipts": (),
            }
            self.registry.advance_execution_phase(root, run_spec, **arguments)
            with self.assertRaisesRegex(ValueError, "replay|expected phase"):
                self.registry.advance_execution_phase(root, run_spec, **arguments)
            quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
            self.assertEqual(list(quarantine.iterdir()), [])

    def test_registry_load_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            expected = self.initialize(root, run_spec)
            execution = root / "audit/v5_2_qd/historical/execution"
            before = {path.relative_to(execution): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in execution.rglob("*")}
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), expected)
            after = {path.relative_to(execution): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in execution.rglob("*")}
            self.assertEqual(after, before)

    def test_schema_is_closed_and_receipts_are_canonical(self) -> None:
        schema = json.loads((ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        for branch in schema["oneOf"]:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["properties"]), set(branch["required"]))
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            payload = json.loads(receipt.read_bytes())
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)
            self.assertEqual(receipt.read_bytes(), canonical_json_bytes(payload) + b"\n")

            failed = self.write_artifact(root, "failed.json", {"status": "FAIL"})
            quarantine = self.registry.quarantine_execution_transition(
                root,
                run_spec,
                attempted_phase="ACQUIRING",
                validation_receipts=(failed,),
            )
            jsonschema.validate(
                json.loads(quarantine.read_bytes()),
                schema,
                format_checker=FORMAT_CHECKER,
            )

            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            transition = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000001-")
            )
            jsonschema.validate(
                json.loads(transition.read_bytes()),
                schema,
                format_checker=FORMAT_CHECKER,
            )

    def test_schema_rejects_impossible_phase_sequence_and_prior_combinations(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            initial = json.loads(receipt.read_bytes())
            impossible = (
                {**initial, "phase": "ACQUIRING"},
                {
                    **initial,
                    "sequence": 1,
                    "prior_receipt_sha256": "0" * 64,
                },
            )
            for payload in impossible:
                with self.subTest(payload=payload), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.validate(
                        payload, schema, format_checker=FORMAT_CHECKER
                    )

    def test_automatic_quarantine_schema_and_runtime_enforce_phase_classification_parity(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            wrong = self.write_artifact(root, "wrong.json", {"wrong": True})
            with self.assertRaises(ValueError):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=wrong,
                    validation_receipts=(),
                )
            quarantine = next(
                (root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()
            )
            payload = json.loads(quarantine.read_bytes())
            payload["error_classification"] = "SOURCE_GATE_EVIDENCE_INVALID"
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    payload, schema, format_checker=FORMAT_CHECKER
                )
            with self.assertRaisesRegex(ValueError, "classification|phase|schema"):
                self.registry._validate_quarantine_receipt(payload)

            invalid_timestamp = json.loads(quarantine.read_bytes())
            invalid_timestamp["recorded_at_utc"] = "2026-02-30T00:00:00.000000Z"
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    invalid_timestamp,
                    schema,
                    format_checker=FORMAT_CHECKER,
                )

    def test_automatic_quarantine_schema_accepts_only_supported_phase_mapping(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            wrong = self.write_artifact(root, "wrong.json", {"wrong": True})
            with self.assertRaises(ValueError):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=wrong,
                    validation_receipts=(),
                )
            quarantine = next(
                (root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()
            )
            acquiring = json.loads(quarantine.read_bytes())
            sources_ready = {
                **acquiring,
                "attempted_phase": "SOURCES_READY",
                "error_classification": "SOURCE_GATE_EVIDENCE_INVALID",
            }
            for valid in (acquiring, sources_ready):
                jsonschema.validate(
                    valid, schema, format_checker=FORMAT_CHECKER
                )
            invalid = (
                {**acquiring, "attempted_phase": "OOF_SEALED"},
                {
                    **acquiring,
                    "error_classification": "SOURCE_GATE_EVIDENCE_INVALID",
                },
                {
                    **sources_ready,
                    "error_classification": "DIRECT_INVENTORY_MISMATCH",
                },
            )
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.validate(
                        payload, schema, format_checker=FORMAT_CHECKER
                    )

    def test_rejects_invalid_receipt_paths_on_emit_and_load(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            bad_inventory = self.write_artifact(
                root, "bad\\inventory.json", {"rows": 1}
            )
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=bad_inventory,
                    validation_receipts=(),
                )
            bad_validation = self.write_artifact(
                root, "bad\\validation.json", {"status": "FAIL"}
            )
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.quarantine_execution_transition(
                    root,
                    run_spec,
                    attempted_phase="ACQUIRING",
                    validation_receipts=(bad_validation,),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )

            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            payload = json.loads(receipt.read_bytes())
            invalid_path = "bad\\inventory.json"
            (root / invalid_path).write_bytes((root / PROTECTED_RELATIVE).read_bytes())
            payload["inventory"]["path"] = invalid_path
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    payload, schema, format_checker=FORMAT_CHECKER
                )
            contents = canonical_json_bytes(payload) + b"\n"
            digest = hashlib.sha256(contents).hexdigest()
            replacement = receipt.with_name(
                f"000000-DESIGN_ACCEPTED-{digest}.json"
            )
            receipt.unlink()
            replacement.write_bytes(contents)
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_dirty_bound_execution_code_but_not_unrelated_files(self) -> None:
        for relative in BOUND_EXECUTION_CODE:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                with (root / relative).open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                    self.initialize(root, run_spec)

        finalization_authority = (
            Path("mlb_backtest/v52_source_finalize.py"),
            Path("mlb_backtest/v52_source_orchestration.py"),
            Path("mlb_backtest/v52_verify_sources.py"),
            Path("mlb_backtest/v52_snapshot_store.py"),
            Path("schemas/v5_2_source_inventory.schema.json"),
            Path("schemas/v5_2_source_review_receipt.schema.json"),
            Path("schemas/v5_2_clean_source_replay_receipt.schema.json"),
            Path("schemas/v5_2_source_receipt_commit.schema.json"),
        )
        for relative in finalization_authority:
            with self.subTest(untracked_relative=relative), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                _git(root, "rm", "--cached", "--", relative.as_posix())
                with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                    self.initialize(root, run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            with (root / BOUND_EXECUTION_CODE[0]).open("a", encoding="utf-8") as handle:
                handle.write("\n")
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})
            with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=inventory,
                    validation_receipts=(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(self.initialize(root, run_spec).phase, "DESIGN_ACCEPTED")

    def test_task7d_runtime_authority_is_exactly_in_production_bound_code(self) -> None:
        required = {path.as_posix() for path in BOUND_EXECUTION_CODE}
        self.assertEqual(set(self.registry._BOUND_EXECUTION_CODE), required)

    def test_bound_execution_import_closure_is_computed_and_exact(self) -> None:
        computed = _repository_local_import_closure(
            ROOT, EXECUTION_IMPORT_ROOT_MODULES
        )
        self.assertEqual(
            computed & PASSIVE_IMPORT_CLOSURE_EXCLUSIONS,
            PASSIVE_IMPORT_CLOSURE_EXCLUSIONS,
        )
        for relative in PASSIVE_IMPORT_CLOSURE_EXCLUSIONS:
            with self.subTest(passive_exclusion=relative):
                self.assertEqual((ROOT / relative).read_bytes(), b"\n")
        reachable = computed - PASSIVE_IMPORT_CLOSURE_EXCLUSIONS
        bound = {Path(path) for path in self.registry._BOUND_EXECUTION_CODE}
        bound_python = {path for path in bound if path.suffix == ".py"}
        self.assertEqual(
            bound_python - reachable,
            set(BOUND_PYTHON_EXTRAS),
        )
        self.assertEqual(
            {path for path in bound if path.parent == Path("schemas")},
            set(BOUND_SCHEMA_PATHS),
        )
        self.assertEqual(
            bound,
            reachable | set(BOUND_PYTHON_EXTRAS) | set(BOUND_SCHEMA_PATHS),
        )
        for relative in self.registry._BOUND_EXECUTION_CODE:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_import_graph_detects_a_new_repository_local_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "mlb_backtest"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            acquisition = package / "acquisition.py"
            acquisition.write_text("VALUE = 1\n", encoding="utf-8")
            before = _repository_local_import_closure(
                root, ("mlb_backtest.acquisition",)
            )

            (package / "new_dependency.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            acquisition.write_text(
                "from . import new_dependency\n", encoding="utf-8"
            )
            after = _repository_local_import_closure(
                root, ("mlb_backtest.acquisition",)
            )

            self.assertNotIn(Path("mlb_backtest/new_dependency.py"), before)
            self.assertIn(Path("mlb_backtest/new_dependency.py"), after)
            simulated_bound = {
                Path("mlb_backtest/__init__.py"),
                Path("mlb_backtest/acquisition.py"),
            }
            self.assertEqual(
                after - simulated_bound,
                {Path("mlb_backtest/new_dependency.py")},
            )

    def test_import_graph_detects_function_local_relative_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "mlb_backtest"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "new_dependency.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (package / "acquisition.py").write_text(
                "def acquire():\n"
                "    if True:\n"
                "        from . import new_dependency\n"
                "    return new_dependency.VALUE\n",
                encoding="utf-8",
            )

            closure = _repository_local_import_closure(
                root, ("mlb_backtest.acquisition",)
            )

            self.assertIn(Path("mlb_backtest/new_dependency.py"), closure)

    def test_task7c_bound_runtime_rejects_dirty_initialize_and_load(self) -> None:
        required = (
            Path("mlb_backtest/v52_game_evidence_inventory.py"),
            Path("mlb_backtest/v52_query_plan.py"),
            Path("mlb_backtest/v52_schedule_inventory.py"),
            Path("schemas/v5_2_game_evidence_inventory.schema.json"),
            Path("schemas/v5_2_schedule_anchor_inventory.schema.json"),
            Path("schemas/v5_2_split_receipt.schema.json"),
        )
        for relative in required:
            for operation in ("initialize", "load"):
                with (
                    self.subTest(relative=relative, operation=operation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root, run_spec = self.make_repo(Path(directory))
                    target = root / relative
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ROOT / relative, target)
                        _git(root, "add", relative.as_posix())
                        _git(root, "commit", "-qm", f"add {relative.name}")
                    if operation == "load":
                        self.initialize(root, run_spec)
                    with target.open("ab") as handle:
                        handle.write(b"\n")
                    with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                        if operation == "initialize":
                            self.initialize(root, run_spec)
                        else:
                            self.registry.load_execution_registry(root, run_spec)

    def test_task7d_timezone_runtime_rejects_dirty_initialize_and_load(self) -> None:
        required = (
            Path("mlb_backtest/v52_timezone_review.py"),
            Path("schemas/v5_2_timezone_candidate.schema.json"),
            Path("schemas/v5_2_timezone_review_receipt.schema.json"),
            Path("schemas/v5_2_timezone_seal_receipt.schema.json"),
            Path("schemas/v5_2_venue_timezones.schema.json"),
        )
        for relative in required:
            for operation in ("initialize", "load"):
                with (
                    self.subTest(relative=relative, operation=operation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root, run_spec = self.make_repo(Path(directory))
                    if operation == "load":
                        self.initialize(root, run_spec)
                    with (root / relative).open("ab") as handle:
                        handle.write(b"\n")
                    with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                        if operation == "initialize":
                            self.initialize(root, run_spec)
                        else:
                            self.registry.load_execution_registry(root, run_spec)

    def test_rejects_committed_bound_code_head_drift_before_checkpoint(self) -> None:
        for phase in ("DESIGN_ACCEPTED", "ACQUIRING"):
            for relative in (
                Path("mlb_backtest/v52_query_plan.py"),
                Path("schemas/v5_2_split_receipt.schema.json"),
            ):
                with (
                    self.subTest(phase=phase, relative=relative),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root, run_spec = self.make_repo(Path(directory))
                    self.initialize(root, run_spec)
                    if phase == "ACQUIRING":
                        self.registry.advance_execution_phase(
                            root,
                            run_spec,
                            expected_phase="DESIGN_ACCEPTED",
                            next_phase="ACQUIRING",
                            inventory_path=root / PROTECTED_RELATIVE,
                            validation_receipts=(),
                        )
                    with (root / relative).open("ab") as handle:
                        handle.write(b"committed drift\n")
                    _git(root, "add", relative.as_posix())
                    _git(root, "commit", "-qm", f"drift {relative.name}")

                    with self.assertRaisesRegex(ValueError, "Git HEAD|snapshot"):
                        self.registry.load_execution_registry(root, run_spec)

    def test_rejects_committed_unrelated_head_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            unrelated = root / "unrelated.txt"
            unrelated.write_text("committed drift\n", encoding="utf-8")
            _git(root, "add", "unrelated.txt")
            _git(root, "commit", "-qm", "unrelated head drift")

            with self.assertRaisesRegex(ValueError, "Git HEAD|snapshot"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_dirty_bound_input_and_identity_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            (root / DESIGN_RELATIVE).write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "dirty|binding|hash|HEAD|index|worktree"
            ):
                self.initialize(root, run_spec)
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            original = self.registry._read_fd_all

            def replace_after_read(descriptor: int) -> bytes:
                data = original(descriptor)
                replacement = receipt.with_suffix(".replacement")
                replacement.write_bytes(data)
                os.replace(replacement, receipt)
                return data

            with patch.object(self.registry, "_read_fd_all", side_effect=replace_after_read), self.assertRaisesRegex((ValueError, RuntimeError), "changed|identity"):
                self.registry.load_execution_registry(root, run_spec)


    def test_plan_a_amendment_binding_is_rehashed_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            (root / AMENDMENT_RELATIVE).write_bytes(
                (root / AMENDMENT_RELATIVE).read_bytes() + b"tampered\n"
            )
            with self.assertRaisesRegex(ValueError, "amendment|binding|hash|dirty|worktree"):
                self.registry.load_execution_registry(root, run_spec)

    def test_sources_ready_allows_multiple_consistent_approved_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            review_marker, replay_one, replay_two = evidence
            original_review = self.source_receipt_from_marker(root, review_marker)
            review_payload = json.loads(original_review.read_bytes())
            review_payload["occurred_at_utc"] = "2026-07-20T00:01:00.000000Z"
            extra_review = self.write_artifact(
                root,
                "audit/v5_2_qd/historical/acquisition/source_reviews/review-extra.json",
                review_payload,
            )
            marker_payload = json.loads(review_marker.read_bytes())
            marker_payload["source_receipt"] = {
                "path": extra_review.relative_to(root).as_posix(),
                "sha256": _sha256(extra_review),
            }
            marker_payload["occurred_at_utc"] = "2026-07-20T00:01:01.000000Z"
            extra_marker = self.write_artifact(
                root,
                "audit/v5_2_qd/historical/acquisition/source_reviews/receipt-extra.commit.json",
                marker_payload,
            )
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=(review_marker, extra_marker, replay_one, replay_two),
            )
            self.assertEqual(state.phase, "SOURCES_READY")

    def test_public_sources_ready_prevalidator_is_read_only_for_valid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            checkpoint = next(
                (root / "audit/v5_2_qd/historical/execution/acquisition_checkpoints").iterdir()
            )
            before = tuple(
                sorted(
                    (path.relative_to(root).as_posix(), path.lstat().st_ino, path.read_bytes())
                    for path in (root / "audit/v5_2_qd/historical/execution").rglob("*")
                    if path.is_file()
                )
            )
            self.registry.validate_sources_ready_evidence_set(
                root,
                run_spec,
                inventory_path=inventory,
                checkpoint_path=checkpoint,
                validation_receipts=evidence,
            )
            after = tuple(
                sorted(
                    (path.relative_to(root).as_posix(), path.lstat().st_ino, path.read_bytes())
                    for path in (root / "audit/v5_2_qd/historical/execution").rglob("*")
                    if path.is_file()
                )
            )
            self.assertEqual(after, before)

    def test_public_sources_ready_prevalidator_final_rebinds_source_dependencies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            checkpoint = next(
                (
                    root
                    / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                ).iterdir()
            )
            dependency_paths = (
                root / self.registry._SOURCE_DISPOSITION_SUCCESS_PATH,
                root / self.registry._SOURCE_DISPOSITION_SIDECAR_PATH,
            )
            for index, path in enumerate(dependency_paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    canonical_json_bytes({"dependency": index}) + b"\n"
                )
            original_validate = self.registry._validate_sources_ready_evidence
            original_terminal = self.registry._terminal_checkpoint_head
            mutated = False

            def validate_with_dependencies(*args: object, **kwargs: object):
                dependencies = original_validate(*args, **kwargs)
                return (
                    *dependencies,
                    *(
                        self.registry._capture_artifact(
                            root, path, "source disposition dependency"
                        )
                        for path in dependency_paths
                    ),
                )

            def mutate_after_validation(*args: object, **kwargs: object):
                nonlocal mutated
                result = original_terminal(*args, **kwargs)
                if not mutated:
                    mutated = True
                    for path in dependency_paths:
                        path.write_bytes(b"attacker replacement\n")
                return result

            with (
                patch.object(
                    self.registry,
                    "_validate_sources_ready_evidence",
                    side_effect=validate_with_dependencies,
                ),
                patch.object(
                    self.registry,
                    "_terminal_checkpoint_head",
                    side_effect=mutate_after_validation,
                ),
                self.assertRaisesRegex(ValueError, "changed|identity|source evidence"),
            ):
                self.registry.validate_sources_ready_evidence_set(
                    root,
                    run_spec,
                    inventory_path=inventory,
                    checkpoint_path=checkpoint,
                    validation_receipts=evidence,
                )
            self.assertTrue(mutated)

    def test_sources_ready_idempotent_retry_revalidates_source_dependencies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            first = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            dependency_paths = (
                root / self.registry._SOURCE_DISPOSITION_SUCCESS_PATH,
                root / self.registry._SOURCE_DISPOSITION_SIDECAR_PATH,
            )
            for index, path in enumerate(dependency_paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    canonical_json_bytes({"dependency": index}) + b"\n"
                )
            original_validate = self.registry._validate_phase_evidence
            mutated = False

            def validate_with_dependencies(*args: object, **kwargs: object):
                nonlocal mutated
                dependencies = original_validate(*args, **kwargs)
                captured = (
                    *dependencies,
                    *(
                        self.registry._capture_artifact(
                            root, path, "source disposition dependency"
                        )
                        for path in dependency_paths
                    ),
                )
                for path in dependency_paths:
                    path.write_bytes(b"attacker replacement\n")
                mutated = True
                return captured

            with (
                patch.object(
                    self.registry,
                    "_validate_phase_evidence",
                    side_effect=validate_with_dependencies,
                ),
                self.assertRaisesRegex(ValueError, "changed|identity|source evidence"),
            ):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertTrue(mutated)
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), first
            )

    def test_public_sources_ready_prevalidator_rejects_duplicate_markers_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            checkpoint = next(
                (root / "audit/v5_2_qd/historical/execution/acquisition_checkpoints").iterdir()
            )
            receipts = root / RECEIPTS_RELATIVE
            before = tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir()))
            with self.assertRaises(ValueError):
                self.registry.validate_sources_ready_evidence_set(
                    root,
                    run_spec,
                    inventory_path=inventory,
                    checkpoint_path=checkpoint,
                    validation_receipts=(evidence[0], evidence[1], evidence[1]),
                )
            self.assertEqual(
                tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir())),
                before,
            )

    def test_sources_ready_exact_retry_returns_existing_without_new_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            arguments = {
                "expected_phase": "ACQUIRING",
                "next_phase": "SOURCES_READY",
                "inventory_path": inventory,
                "validation_receipts": evidence,
            }
            advanced = self.registry.advance_execution_phase(root, run_spec, **arguments)
            receipts = root / RECEIPTS_RELATIVE
            before = tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir()))
            existing = self.registry.advance_execution_phase(root, run_spec, **arguments)
            self.assertEqual(existing, advanced)
            self.assertEqual(
                tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir())),
                before,
            )

    def test_sources_ready_retry_rejects_reordered_or_changed_evidence_without_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            arguments = {
                "expected_phase": "ACQUIRING",
                "next_phase": "SOURCES_READY",
                "inventory_path": inventory,
                "validation_receipts": evidence,
            }
            advanced = self.registry.advance_execution_phase(root, run_spec, **arguments)
            receipts = root / RECEIPTS_RELATIVE
            before = tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir()))
            with self.assertRaisesRegex(ValueError, "retry evidence differs"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    **{**arguments, "validation_receipts": (evidence[0], evidence[2], evidence[1])},
                )
            for changed in (evidence[:2], (*evidence, evidence[0])):
                with self.subTest(changed=len(changed)), self.assertRaises(ValueError):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        **{**arguments, "validation_receipts": changed},
                    )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), advanced)
            self.assertEqual(tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir())), before)
            self.assertEqual(list((root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()), [])
            inventory.write_bytes(inventory.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "retry evidence differs|phase inventory"):
                self.registry.advance_execution_phase(root, run_spec, **arguments)
            self.assertEqual(tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir())), before)
            self.assertEqual(list((root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()), [])

    def test_concurrent_identical_sources_ready_requests_converge_to_one_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            barrier = threading.Barrier(2)

            def advance_once() -> object:
                barrier.wait()
                return self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _unused: advance_once(), range(2)))
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(outcomes[0].phase, "SOURCES_READY")
            self.assertEqual(
                len([path for path in (root / RECEIPTS_RELATIVE).iterdir() if "SOURCES_READY" in path.name]),
                1,
            )

    def test_concurrent_different_sources_ready_requests_leave_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            barrier = threading.Barrier(2)

            def advance_once(receipts: tuple[Path, ...]) -> object:
                barrier.wait()
                try:
                    return self.registry.advance_execution_phase(
                        root, run_spec, expected_phase="ACQUIRING", next_phase="SOURCES_READY",
                        inventory_path=inventory, validation_receipts=receipts,
                    )
                except ValueError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(advance_once, (evidence, (evidence[0], evidence[2], evidence[1]))))
            self.assertEqual(
                sum(isinstance(item, self.registry.ExecutionRegistryState) for item in outcomes),
                1,
            )
            self.assertEqual(sum(isinstance(item, ValueError) for item in outcomes), 1)
            self.assertEqual(
                len([path for path in (root / RECEIPTS_RELATIVE).iterdir() if "SOURCES_READY" in path.name]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
