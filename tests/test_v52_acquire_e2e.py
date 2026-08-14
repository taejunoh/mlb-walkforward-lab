from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from mlb_backtest.protocols import load_run_spec, load_source_run_spec
from mlb_backtest.v52_evidence import AttemptCapture, LogicalQuery
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from tests.test_v52_legacy_decision_inventory import (
    INVENTORY_PATH,
    LEGACY_ROOT,
    _legacy_decision_repository,
    _verified_legacy_decision_authority,
)
from tests.test_v52_source_schedule import (
    SOURCE_AUTHORITY_ROOT,
    _boxscore_raw,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
PROTECTED_DUPLICATES = (
    ROOT / "mlb_backtest/v52_query_plan 2.py",
    ROOT / "reports/v5_2_qd/walkforward_500_preflight 2.md",
    ROOT / "schemas/v5_2_preflight.schema 2.json",
    ROOT / "tests/test_v52_query_plan 2.py",
)
SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
OLD_QUERY_ID = "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8"
OLD_QUERY_REGISTRATION_PATH = (
    "data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed/"
    f"{OLD_QUERY_ID}/query.json"
)
OLD_QUARANTINED_RAW_PATH = (
    "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/"
    f"{OLD_QUERY_ID}/6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0.raw"
)


def _canonical_file(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(child) for child in value]
    return value


def _snapshot_tree(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _protected_duplicate_snapshot() -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_ino)
        for path in PROTECTED_DUPLICATES
        if path.exists()
    }


def _bind_runtime(root: Path) -> None:
    shutil.copytree(ROOT / "mlb_backtest", root / "mlb_backtest")
    for source in (ROOT / "schemas").iterdir():
        target = root / "schemas" / source.name
        if not target.exists():
            shutil.copy2(source, target)
    subprocess.run(
        ["git", "add", "mlb_backtest", "schemas", INVENTORY_PATH.as_posix()],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "bind acquisition runtime"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _played_schedule(query: LogicalQuery) -> bytes:
    payload = json.loads((FIXTURES / "schedule_mini.json").read_bytes())
    game = payload["dates"][0]["games"][0]
    year = query.requested_range["year"]
    official_date = query.requested_range["start_date"]
    game["gamePk"] = int(year) * 1000 + 1
    game["officialDate"] = official_date
    game["gameDate"] = f"{official_date}T17:05:00Z"
    game["teams"]["away"]["team"]["id"] = 10
    game["teams"]["home"]["team"]["id"] = 20
    game["venue"]["id"] = 31
    return _canonical_file(
        {
            "dates": [
                {
                    "date": official_date,
                    "games": [game],
                    "totalGames": 1,
                }
            ],
            "totalGames": 1,
        }
    )


def _venue_payload(venue_id: int) -> bytes:
    payload = json.loads((FIXTURES / "venues_mini.json").read_bytes())
    payload["venues"] = [
        venue for venue in payload["venues"] if venue["id"] == venue_id
    ]
    if len(payload["venues"]) != 1:
        raise AssertionError("fixture venue is missing")
    return _canonical_file(payload)


def _response_body(query: LogicalQuery) -> bytes:
    if query.endpoint.endswith("/schedule"):
        return _played_schedule(query)
    if query.endpoint.endswith("/boxscore"):
        return _boxscore_raw()
    if query.endpoint.endswith("/teams"):
        return (FIXTURES / "team_mini.json").read_bytes()
    if "/venues/" in query.endpoint:
        return _venue_payload(int(query.requested_range["venue_id"]))
    if "/people/" in query.endpoint:
        return b'{"stats":[]}'
    if "statcast_search/csv" in query.endpoint:
        return (
            (FIXTURES / "statcast_mini.csv").read_bytes().splitlines(keepends=True)[0]
        )
    raise AssertionError(f"unexpected fixture query: {query.source} {query.endpoint}")


class DeterministicFetcher:
    def __init__(self) -> None:
        self.query_ids: list[str] = []

    def __call__(
        self, query: LogicalQuery, *, timeout: float, max_bytes: int
    ) -> AttemptCapture:
        from mlb_backtest.v52_evidence import logical_query_id

        if timeout != 90.0 or max_bytes <= 0:
            raise AssertionError("runner transport limits differ")
        self.query_ids.append(logical_query_id(query))
        return AttemptCapture(
            started_at_utc="2026-07-16T12:00:00.000000Z",
            ended_at_utc="2026-07-16T12:00:00.000000Z",
            final_url=query.endpoint,
            http_status=200,
            response_headers=(),
            body=_response_body(query),
            error_classification=None,
            timed_out=False,
        )


def _fixture_repository(parent: Path) -> tuple[Path, Path, str]:
    root, inventory_path, digest = _legacy_decision_repository(
        parent, production_shaped=True
    )
    _bind_runtime(root)
    return root, inventory_path, digest


@dataclasses.dataclass
class _FixtureAcquisitionValidationState:
    successful_validations: int = 0
    registry_contract_reads: int = 0
    rebound_contract_contents: bytes | None = None
    game_authority_context_attempts: int = 0
    game_authority_original_returns: int = 0
    game_authority_rebounds: int = 0
    game_authority_rebound_contract_contents: bytes | None = None


@contextmanager
def _authenticated_fixture_acquisition(root: Path, digest: str):
    """Rebind only the synthetic fixture's in-memory acquisition contract.

    The production request validator still runs first, so canonical paths,
    run-spec bindings, and current-HEAD checks remain authoritative.  Once
    validation succeeds for this exact fixture root, return a copied contract
    with the fixture inventory digest without changing authority files.
    """
    from mlb_backtest import (
        execution_registry,
        v52_acquire,
        v52_game_evidence_inventory,
    )

    original_validator = v52_acquire._validate_request_contract
    original_reader = execution_registry._read_path
    original_game_authority_context = v52_game_evidence_inventory._authority_context
    expected_root = root.resolve()
    expected_contract_path = expected_root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE
    state = _FixtureAcquisitionValidationState()

    def validate(request):
        validated_root, contract, _validated_contents = original_validator(request)
        if (
            validated_root != expected_root
            or Path(request.root).resolve() != expected_root
        ):
            raise AssertionError(
                "fixture acquisition seam received an unexpected root"
            )
        state.successful_validations += 1
        rebound_contract = copy.deepcopy(contract)
        legacy = rebound_contract.get("legacy_protection")
        if not isinstance(legacy, dict):
            raise AssertionError("fixture acquisition contract lacks legacy protection")
        legacy["protected_inventory_sha256"] = digest
        rebound_contents = canonical_json_bytes(rebound_contract) + b"\n"
        if state.rebound_contract_contents is None:
            state.rebound_contract_contents = rebound_contents
        elif state.rebound_contract_contents != rebound_contents:
            raise AssertionError("fixture acquisition contract rebound drifted")
        return (
            validated_root,
            rebound_contract,
            state.rebound_contract_contents,
        )

    def read_path(root_arg: Path, path: Path, label: str):
        if (
            state.rebound_contract_contents is not None
            and Path(root_arg).resolve() == expected_root
            and Path(path) == expected_contract_path
            and label == "acquisition contract"
        ):
            state.registry_contract_reads += 1
            return (
                v52_acquire._ACQUISITION_CONTRACT_RELATIVE.as_posix(),
                state.rebound_contract_contents,
            )
        return original_reader(root_arg, path, label)

    def game_authority_context(
        root_arg: Path,
        run_spec_path: Path | None = None,
        acquisition_contract_path: Path | None = None,
    ):
        state.game_authority_context_attempts += 1
        context = original_game_authority_context(
            root_arg,
            run_spec_path,
            acquisition_contract_path,
        )
        state.game_authority_original_returns += 1
        if Path(root_arg).resolve() != expected_root:
            return context
        if context.repository_root != expected_root:
            raise AssertionError("fixture game authority seam received an unexpected root")
        rebound_contract = _plain_json(context.contract)
        if not isinstance(rebound_contract, dict):
            raise AssertionError("fixture game evidence contract is not an object")
        legacy = rebound_contract.get("legacy_protection")
        if not isinstance(legacy, dict):
            raise AssertionError("fixture game evidence contract lacks legacy protection")
        legacy["protected_inventory_sha256"] = digest
        rebound_contents = canonical_json_bytes(rebound_contract) + b"\n"
        if (
            state.game_authority_rebound_contract_contents is not None
            and state.game_authority_rebound_contract_contents != rebound_contents
        ):
            raise AssertionError("fixture game evidence contract rebound drifted")
        state.game_authority_rebound_contract_contents = rebound_contents
        state.game_authority_rebounds += 1
        return dataclasses.replace(context, contract=rebound_contract)

    with (
        patch.object(v52_acquire, "_validate_request_contract", side_effect=validate),
        patch.object(execution_registry, "_read_path", side_effect=read_path),
        patch.object(
            v52_game_evidence_inventory,
            "_authority_context",
            side_effect=game_authority_context,
        ),
    ):
        yield state


def _acquire(
    root: Path,
    inventory_path: Path,
    fetcher: DeterministicFetcher,
    *,
    stop_after: str | None = None,
):
    from mlb_backtest.v52_acquire import acquire_sources

    return acquire_sources(
        root,
        load_run_spec(root / RUN_SPEC_RELATIVE),
        years=range(2018, 2025),
        mode="hybrid",
        resume=True,
        stop_after=stop_after,
        legacy_root=root / LEGACY_ROOT,
        protected_inventory_path=inventory_path,
        fetcher=fetcher,
    )


class V52AcquirePublicE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        from mlb_backtest import execution_registry, v52_query_plan

        # The complete 44-file runtime closure has dedicated registry coverage.
        # Keep this multi-checkpoint integration fixture bounded while retaining
        # the real Git snapshot, receipt, and replay implementation.
        self.bound_schema_paths = tuple(
            relative
            for relative in execution_registry._BOUND_EXECUTION_CODE
            if Path(relative).parent == Path("schemas")
        )
        closure = patch(
            "mlb_backtest.execution_registry._BOUND_EXECUTION_CODE",
            ("mlb_backtest/execution_registry.py", *self.bound_schema_paths),
        )
        closure.start()
        self.addCleanup(closure.stop)
        original_head_blob = v52_query_plan._verify_head_blob
        verified_blobs: dict[tuple[Path, Path, str, object], str] = {}
        self.head_blob_original_calls: list[tuple[Path, Path, str, object]] = []

        def verify_head_blob_once(
            root: Path,
            relative: Path,
            contents: bytes,
            label: str,
            *,
            expected_blob: object | None = None,
        ) -> str:
            key = (
                Path(root).resolve(),
                Path(relative),
                hashlib.sha256(contents).hexdigest(),
                expected_blob,
            )
            if key not in verified_blobs:
                result = original_head_blob(
                    root,
                    relative,
                    contents,
                    label,
                    expected_blob=expected_blob,
                )
                verified_blobs[key] = result
                self.head_blob_original_calls.append(key)
            return verified_blobs[key]

        head_blob = patch(
            "mlb_backtest.v52_query_plan._verify_head_blob",
            side_effect=verify_head_blob_once,
        )
        head_blob.start()
        self.addCleanup(head_blob.stop)
        original_loader = v52_query_plan.load_query_batch

        def memoized_batch_loader(*, loader=original_loader):
            validated_batches: dict[tuple[Path, str, bool], object] = {}

            def load_once(
                root: Path,
                batch_id: str,
                *,
                read_only_progress: bool = False,
            ):
                key = (Path(root).resolve(), batch_id, read_only_progress)
                if key not in validated_batches:
                    validated_batches[key] = loader(
                        root,
                        batch_id,
                        read_only_progress=read_only_progress,
                    )
                return validated_batches[key]

            return patch(
                "mlb_backtest.v52_query_plan.load_query_batch",
                side_effect=load_once,
            )

        self.memoized_batch_loader = memoized_batch_loader

    def test_bounded_runtime_closure_keeps_current_schema_bindings(self) -> None:
        from mlb_backtest import execution_registry

        self.assertEqual(len(self.bound_schema_paths), 19)
        self.assertTrue(
            {
                "schemas/v5_2_acquisition_contract.schema.json",
                "schemas/v5_2_derived_disposition_sidecar.schema.json",
            }
            <= set(self.bound_schema_paths)
        )
        self.assertEqual(
            execution_registry._BOUND_EXECUTION_CODE,
            ("mlb_backtest/execution_registry.py", *self.bound_schema_paths),
        )

    def test_absent_protected_duplicate_does_not_invalidate_fixture_but_existing_mutations_remain_detected(
        self,
    ) -> None:
        existing_payload = b"existing duplicate fixture\n"
        mutated_payload = b"mutated duplicate fixture\n"
        created_payload = b"created duplicate fixture\n"
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            existing = fixture_root / "existing.py"
            absent = fixture_root / "absent.py"
            existing.write_bytes(existing_payload)
            existing_inode = existing.stat().st_ino

            with patch(
                __name__ + ".PROTECTED_DUPLICATES", (existing, absent)
            ):
                before = _protected_duplicate_snapshot()
                self.assertNotIn(absent, before)
                self.assertEqual(before[existing], (existing_payload, existing_inode))

                existing.write_bytes(mutated_payload)
                after_mutation = _protected_duplicate_snapshot()
                self.assertNotEqual(before, after_mutation)
                self.assertEqual(
                    after_mutation[existing], (mutated_payload, existing_inode)
                )

                absent.write_bytes(created_payload)
                after_creation = _protected_duplicate_snapshot()
                self.assertNotEqual(after_mutation, after_creation)
                self.assertEqual(
                    after_creation[absent], (created_payload, absent.stat().st_ino)
                )

    def test_superseding_pair_rejects_rawless_root_before_checkpoint_io(self) -> None:
        from mlb_backtest import execution_registry
        from mlb_backtest.v52_acquire import TrustIntegrityError, acquire_sources

        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)
        kwargs = {
            "years": range(2018, 2025),
            "mode": "hybrid",
            "resume": False,
            "stop_after": None,
            "legacy_root": ROOT / LEGACY_ROOT,
            "protected_inventory_path": ROOT / INVENTORY_PATH,
            "plan_only": False,
        }
        self.assertFalse((ROOT / LEGACY_ROOT).exists())

        # Keep the frozen-pair rejection separate from the raw-less root
        # rejection: both must fail before any registry/checkpoint I/O, but
        # they identify different authority violations.
        cases = (
            ("rawless root", source_contract_path, "legacy root is unavailable"),
            (
                "frozen source authority pair",
                ROOT / "protocols/v5_2_qd/historical_source_acquisition.json",
                "source authority authentication failed",
            ),
        )
        for label, contract_path, error_message in cases:
            with self.subTest(label):
                with (
                    patch.object(
                        execution_registry,
                        "initialize_execution_registry",
                    ) as initialize,
                    patch.object(
                        execution_registry,
                        "load_execution_registry",
                    ) as load_registry,
                    patch.object(
                        execution_registry,
                        "load_latest_acquisition_checkpoint",
                    ) as load_checkpoint,
                    patch.object(
                        execution_registry,
                        "publish_acquisition_checkpoint",
                    ) as publish_checkpoint,
                ):
                    with self.assertRaisesRegex(
                        TrustIntegrityError,
                        rf"^{error_message}$",
                    ):
                        acquire_sources(
                            ROOT,
                            source_run,
                            acquisition_contract_path=contract_path,
                            **kwargs,
                        )
                initialize.assert_not_called()
                load_registry.assert_not_called()
                load_checkpoint.assert_not_called()
                publish_checkpoint.assert_not_called()

        self.assertTrue(
            {
                "schemas/v5_2_clean_source_replay_receipt.schema.json",
                "schemas/v5_2_source_receipt_commit.schema.json",
                "schemas/v5_2_source_review_receipt.schema.json",
            }
            <= set(self.bound_schema_paths)
        )

    def test_new_checkpoint_never_contains_old_query_id_or_old_artifact_binding(
        self,
    ) -> None:
        from mlb_backtest import execution_registry

        schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json").read_bytes()
        )
        with self.subTest("schema branch"):
            self.assertIn("oneOf", schema)
            if "oneOf" in schema:
                source_branches = [
                    branch
                    for branch in schema["oneOf"]
                    if branch.get("properties", {})
                    .get("run_id", {})
                    .get("const")
                    == SOURCE_RUN_ID
                ]
                self.assertEqual(len(source_branches), 1)
                if source_branches:
                    source_branch = source_branches[0]
                    serialized_branch = canonical_json_bytes(source_branch)
                    for forbidden in (
                        OLD_QUERY_ID,
                        OLD_QUERY_REGISTRATION_PATH,
                        OLD_QUARANTINED_RAW_PATH,
                        "data/v5_2_qd/historical/raw/quarantine/",
                    ):
                        self.assertNotIn(forbidden.encode(), serialized_branch)
                    self.assertIn(
                        "dispositioned_query_ids", source_branch["required"]
                    )
                    self.assertEqual(
                        source_branch["properties"]["run_id"],
                        {"const": SOURCE_RUN_ID},
                    )

        # Runtime boundary: a source checkpoint may not smuggle the immutable
        # old query registration or its old-root artifact into any population.
        from tests.test_execution_registry import _source_checkpoint_payload

        source_run = load_source_run_spec(
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        old_paths = {
            "execution": (
                "audit/v5_2_qd/historical/execution/phase_receipts/forged.json"
            ),
            "acquisition": (
                "audit/v5_2_qd/historical/acquisition/query_batches/forged.json"
            ),
            "raw": OLD_QUERY_REGISTRATION_PATH,
        }
        binding_factories = {
            "query_evidence_receipts": lambda path: {
                "path": path,
                "query_id": "1" * 64,
                "receipt_kind": "SUCCESS",
                "sha256": "2" * 64,
            },
            "stage_evidence_receipts": lambda path: {
                "path": path,
                "sha256": "3" * 64,
            },
            "query_batches": lambda path: {
                "batch_id": "4" * 64,
                "path": path,
                "sha256": "5" * 64,
                "stage": "STATIC_SCHEDULES",
            },
            "split_receipts": lambda path: {
                "parent_query_id": "6" * 64,
                "path": path,
                "sha256": "7" * 64,
                "split_receipt_id": "8" * 64,
            },
        }
        for field, make_binding in binding_factories.items():
            for namespace, old_path in old_paths.items():
                with self.subTest(field=field, namespace=namespace):
                    contaminated = _source_checkpoint_payload()
                    if field in {
                        "stage_evidence_receipts",
                        "split_receipts",
                    }:
                        contaminated["stage"] = "REMAINING"
                    contaminated[field] = [make_binding(old_path)]
                    with self.assertRaisesRegex(
                        ValueError,
                        "old|artifact|binding|namespace|path",
                    ):
                        execution_registry._validate_checkpoint_payload(
                            contaminated, source_run
                        )

    def test_real_public_flow_stops_for_manual_gate_then_reaches_terminal_replay(
        self,
    ) -> None:
        from mlb_backtest.execution_registry import (
            load_execution_registry,
            load_latest_acquisition_checkpoint,
        )
        from mlb_backtest.v52_acquire import OperationalBlock, TrustIntegrityError
        from mlb_backtest.v52_timezone_review import (
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )

        duplicate_before = _protected_duplicate_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            fixture_authority = _authenticated_fixture_acquisition(root, digest)
            fixture_validation = fixture_authority.__enter__()
            self.addCleanup(fixture_authority.__exit__, None, None, None)
            legacy_authority = _verified_legacy_decision_authority(root, digest)
            legacy_authority.__enter__()
            self.addCleanup(legacy_authority.__exit__, None, None, None)
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            fetcher = DeterministicFetcher()
            with (
                self.memoized_batch_loader(),
                _authenticated_fixture_acquisition(root, digest),
            ):
                reference = _acquire(
                    root,
                    inventory_path,
                    fetcher,
                    stop_after="reference-inputs",
                )
            self.assertEqual(load_execution_registry(root, run_spec).phase, "ACQUIRING")
            self.assertEqual(
                load_latest_acquisition_checkpoint(root, run_spec).stage,
                "REFERENCE_INPUTS",
            )
            self.assertEqual(reference.pending, 0)
            self.assertEqual(reference.quarantined, 0)
            self.assertEqual(reference.promoted, 0)
            self.assertEqual(reference.fetched, 22)
            self.assertEqual(reference.resumed, 0)
            self.assertEqual(reference.network_attempts, 22)
            self.assertEqual(reference.planned_promoted, 1)
            self.assertEqual(reference.planned_fetch_required, 47)
            self.assertEqual(len(reference.query_batch_sha256s), 2)

            with (
                self.memoized_batch_loader(),
                _authenticated_fixture_acquisition(root, digest),
                self.assertRaisesRegex(OperationalBlock, "review|seal"),
            ):
                _acquire(root, inventory_path, fetcher)
            candidate_path = (
                root / "audit/v5_2_qd/historical/acquisition/"
                "venue_timezone_candidate.json"
            )
            candidate = json.loads(candidate_path.read_bytes())
            self.assertEqual(candidate["venue_scope"], [31])
            self.assertTrue(
                all(row["time_zone"] is None for row in candidate["timezone_rows"])
            )
            for row in candidate["timezone_rows"]:
                row["time_zone"] = "America/New_York"
            candidate_path.write_bytes(_canonical_file(candidate))

            review_root = Path("audit/v5_2_qd/historical/acquisition/timezone_reviews")
            coverage = write_timezone_review_receipt(
                root,
                candidate_path=candidate_path,
                output=review_root / "coverage.json",
                reviewer_identity="fixture-completeness-reviewer",
                reviewer_role="TIMEZONE_COVERAGE_REVIEW",
                review_scope=[31],
                decision="APPROVED",
            )
            semantic = write_timezone_review_receipt(
                root,
                candidate_path=candidate_path,
                output=review_root / "semantic.json",
                reviewer_identity="fixture-semantic-reviewer",
                reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                review_scope=[31],
                decision="APPROVED",
            )
            seal_timezone_mapping(
                root,
                candidate_path=candidate_path,
                review_receipt_paths=(coverage, semantic),
                output=Path("config/v5_2_qd/venue_timezones.json"),
                receipt_output=Path(
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"
                ),
            )

            with (
                self.memoized_batch_loader(),
                _authenticated_fixture_acquisition(root, digest),
            ):
                terminal = _acquire(root, inventory_path, fetcher)
            terminal_checkpoint = load_latest_acquisition_checkpoint(root, run_spec)
            self.assertEqual(terminal_checkpoint.stage, "REMAINING")
            self.assertEqual(load_execution_registry(root, run_spec).phase, "ACQUIRING")
            self.assertEqual(terminal.promoted, 1)
            self.assertEqual(terminal.pending, 0)
            self.assertEqual(terminal.quarantined, 0)
            self.assertEqual(terminal.fetched, 51)
            self.assertEqual(terminal.resumed, 29)
            self.assertEqual(terminal.network_attempts, 51)
            self.assertEqual(terminal.planned_promoted, 1)
            self.assertEqual(terminal.planned_fetch_required, 47)
            self.assertEqual(len(terminal.query_batch_sha256s), 4)
            self.assertEqual(
                terminal.query_batch_sha256s,
                terminal_checkpoint.query_batch_ids,
            )
            self.assertEqual(
                terminal.query_batch_sha256s[:2],
                reference.query_batch_sha256s,
            )
            network_calls = len(fetcher.query_ids)
            self.assertEqual(network_calls, 73)
            self.assertEqual(
                len(
                    list(
                        (root / "data/v5_2_qd/historical/raw/queries").rglob(
                            "success.json"
                        )
                    )
                ),
                81,
            )
            from mlb_backtest.execution_registry import advance_execution_phase
            from tests.test_execution_registry import ExecutionRegistryTest

            inventory, source_receipts = (
                ExecutionRegistryTest().write_real_source_evidence(
                    root,
                    terminal_checkpoint=False,
                )
            )
            advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=source_receipts,
            )
            self.assertEqual(
                load_execution_registry(root, run_spec).phase,
                "SOURCES_READY",
            )
            terminal_tree = _snapshot_tree(root)

            with _authenticated_fixture_acquisition(root, digest):
                replay = _acquire(root, inventory_path, fetcher)
            self.assertEqual(len(fetcher.query_ids), 73)
            self.assertEqual(_snapshot_tree(root), terminal_tree)
            self.assertEqual(replay.query_batch_sha256s, terminal.query_batch_sha256s)
            self.assertEqual(replay.promoted, 0)
            self.assertEqual(replay.fetched, 0)
            self.assertEqual(replay.resumed, 81)
            self.assertEqual(replay.network_attempts, 0)
            self.assertEqual(replay.planned_promoted, 1)
            self.assertEqual(replay.planned_fetch_required, 47)
            self.assertEqual(replay.pending, 0)
            self.assertEqual(replay.quarantined, 0)

            success_path = next(
                (root / "data/v5_2_qd/historical/raw/queries").rglob("success.json")
            )
            success_path.write_bytes(success_path.read_bytes() + b" ")
            tampered_tree = _snapshot_tree(root)
            with self.assertRaisesRegex(
                TrustIntegrityError,
                "^execution registry authentication failed$",
            ) as tamper_error:
                with _authenticated_fixture_acquisition(root, digest) as validation:
                    _acquire(root, inventory_path, fetcher)
            self.assertEqual(validation.successful_validations, 1)
            self.assertIsInstance(tamper_error.exception.__cause__, ValueError)
            self.assertEqual(
                str(tamper_error.exception.__cause__),
                "success receipt must have exactly one final newline",
            )
            self.assertEqual(_snapshot_tree(root), tampered_tree)
            self.assertEqual(len(fetcher.query_ids), 73)

        self.assertEqual(_protected_duplicate_snapshot(), duplicate_before)
        self.assertGreater(fixture_validation.registry_contract_reads, 0)

    def test_fixture_registry_reads_rebound_contract_for_task0_lineage(self) -> None:
        from mlb_backtest import v52_acquire
        from mlb_backtest.execution_registry import initialize_execution_registry

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            design_path, plan_path, contract_path = (
                v52_acquire._execution_authority_paths_for_run_spec(run_spec, root)
            )
            with _authenticated_fixture_acquisition(root, digest) as validation:
                v52_acquire.acquire_sources(
                    root,
                    run_spec,
                    years=range(2018, 2025),
                    mode="hybrid",
                    resume=False,
                    stop_after=None,
                    legacy_root=root / LEGACY_ROOT,
                    protected_inventory_path=inventory_path,
                    plan_only=True,
                )
                state = initialize_execution_registry(
                    root,
                    run_spec,
                    design_path=design_path,
                    plan_path=plan_path,
                    acquisition_contract_path=contract_path,
                )

            self.assertEqual(state.phase, "DESIGN_ACCEPTED")
            self.assertGreater(validation.registry_contract_reads, 0)
            self.assertIsNotNone(validation.rebound_contract_contents)

    def test_game_evidence_physical_authority_rejects_synthetic_fixture_digest(self) -> None:
        from mlb_backtest import v52_acquire, v52_game_evidence_inventory as inventory_module
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _fixture_digest = _fixture_repository(Path(directory))
            context = inventory_module._authority_context(
                root,
                root / RUN_SPEC_RELATIVE,
                root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE,
            )

            with self.assertRaisesRegex(
                ProtectedLegacyIntegrityError,
                "PROTECTED_INVENTORY_INVALID",
            ):
                inventory_module._context_legacy_evidence(root, context, None)

    def test_game_evidence_fixture_authority_rebinds_digest_after_original_context(self) -> None:
        from mlb_backtest import v52_acquire, v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, fixture_digest = _fixture_repository(Path(directory))
            contract_path = root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE
            physical_contract = contract_path.read_bytes()
            physical_contract_sha256 = hashlib.sha256(physical_contract).hexdigest()
            expected_contract = json.loads(physical_contract)
            expected_contract["legacy_protection"][
                "protected_inventory_sha256"
            ] = fixture_digest
            physical_context = inventory_module._authority_context(
                root,
                root / RUN_SPEC_RELATIVE,
                contract_path,
            )

            with _authenticated_fixture_acquisition(root, fixture_digest) as validation:
                context = inventory_module._authority_context(
                    root,
                    root / RUN_SPEC_RELATIVE,
                    contract_path,
                )
                legacy = inventory_module._context_legacy_evidence(root, context, None)

            self.assertIsNotNone(legacy)
            self.assertEqual(legacy.inventory_sha256, fixture_digest)
            self.assertEqual(context.contract, expected_contract)
            self.assertEqual(context.acquisition_contract_sha256, physical_contract_sha256)
            self.assertEqual(context.acquisition_contract_path, contract_path)
            for field in (
                "repository_root",
                "authority",
                "authority_path",
                "authority_sha256",
                "acquisition_contract_path",
                "acquisition_contract_sha256",
                "run_id",
                "raw_root",
                "sources_root",
                "acquisition_audit_root",
                "execution_audit_root",
                "report_root",
            ):
                self.assertEqual(getattr(context, field), getattr(physical_context, field))
            self.assertEqual(validation.game_authority_context_attempts, 1)
            self.assertEqual(validation.game_authority_original_returns, 1)
            self.assertEqual(validation.game_authority_rebounds, 1)
            self.assertEqual(contract_path.read_bytes(), physical_contract)

    def test_game_evidence_fixture_authority_wrong_digest_fails_closed(self) -> None:
        from mlb_backtest import v52_acquire, v52_game_evidence_inventory as inventory_module
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _fixture_digest = _fixture_repository(Path(directory))
            contract_path = root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE
            physical_contract = contract_path.read_bytes()
            with _authenticated_fixture_acquisition(root, "0" * 64) as validation:
                context = inventory_module._authority_context(
                    root,
                    root / RUN_SPEC_RELATIVE,
                    contract_path,
                )
                with self.assertRaisesRegex(
                    ProtectedLegacyIntegrityError,
                    "PROTECTED_INVENTORY_INVALID",
                ):
                    inventory_module._context_legacy_evidence(root, context, None)

            self.assertEqual(validation.game_authority_context_attempts, 1)
            self.assertEqual(validation.game_authority_original_returns, 1)
            self.assertEqual(validation.game_authority_rebounds, 1)
            self.assertEqual(contract_path.read_bytes(), physical_contract)

    def test_game_evidence_authority_wrong_root_is_not_rebound(self) -> None:
        from mlb_backtest import v52_acquire, v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, fixture_digest = _fixture_repository(Path(directory))
            contract_path = root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE
            physical_contract = contract_path.read_bytes()
            with _authenticated_fixture_acquisition(root, fixture_digest) as validation:
                with self.assertRaisesRegex(ValueError, "repository root"):
                    inventory_module._authority_context(
                        root.parent,
                        root / RUN_SPEC_RELATIVE,
                        contract_path,
                    )

            self.assertEqual(validation.game_authority_context_attempts, 1)
            self.assertEqual(validation.game_authority_original_returns, 0)
            self.assertEqual(validation.game_authority_rebounds, 0)
            self.assertEqual(contract_path.read_bytes(), physical_contract)

    def test_fixture_registry_seam_delegates_unexpected_reads(self) -> None:
        from mlb_backtest import execution_registry, v52_acquire

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            arguments = dict(
                years=range(2018, 2025),
                mode="hybrid",
                resume=False,
                stop_after=None,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=inventory_path,
                plan_only=True,
            )
            with _authenticated_fixture_acquisition(root, digest) as validation:
                v52_acquire.acquire_sources(root, run_spec, **arguments)
                expected_relative = v52_acquire._ACQUISITION_CONTRACT_RELATIVE
                expected_path = root / expected_relative
                disk_contents = expected_path.read_bytes()

                wrong_root = execution_registry._read_path(
                    ROOT,
                    ROOT / expected_relative,
                    "acquisition contract",
                )
                wrong_path = execution_registry._read_path(
                    root,
                    root / RUN_SPEC_RELATIVE,
                    "acquisition contract",
                )
                wrong_label = execution_registry._read_path(
                    root,
                    expected_path,
                    "run spec",
                )

            self.assertEqual(wrong_root[1], (ROOT / expected_relative).read_bytes())
            self.assertEqual(wrong_path[1], (root / RUN_SPEC_RELATIVE).read_bytes())
            self.assertEqual(wrong_label[1], disk_contents)
            self.assertEqual(validation.registry_contract_reads, 0)

    def test_fixture_registry_rejects_wrong_rebound_digest(self) -> None:
        from mlb_backtest import v52_acquire
        from mlb_backtest.execution_registry import initialize_execution_registry

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            design_path, plan_path, contract_path = (
                v52_acquire._execution_authority_paths_for_run_spec(run_spec, root)
            )
            with _authenticated_fixture_acquisition(root, digest) as validation:
                v52_acquire.acquire_sources(
                    root,
                    run_spec,
                    years=range(2018, 2025),
                    mode="hybrid",
                    resume=False,
                    stop_after=None,
                    legacy_root=root / LEGACY_ROOT,
                    protected_inventory_path=inventory_path,
                    plan_only=True,
                )
                rebound = json.loads(validation.rebound_contract_contents or b"{}")
                rebound["legacy_protection"]["protected_inventory_sha256"] = "0" * 64
                validation.rebound_contract_contents = _canonical_file(rebound)
                with self.assertRaisesRegex(
                    ValueError, "^Task0 lineage differs from acquisition contract$"
                ):
                    initialize_execution_registry(
                        root,
                        run_spec,
                        design_path=design_path,
                        plan_path=plan_path,
                        acquisition_contract_path=contract_path,
                    )

    def test_fixture_disk_contract_tamper_stops_before_registry_seam(self) -> None:
        from mlb_backtest import v52_acquire
        from mlb_backtest.v52_acquire import TrustIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            contract_path = root / v52_acquire._ACQUISITION_CONTRACT_RELATIVE
            contract_path.write_bytes(contract_path.read_bytes() + b" ")
            with _authenticated_fixture_acquisition(root, digest) as validation:
                with self.assertRaisesRegex(
                    TrustIntegrityError, "acquisition contract is not canonical JSON"
                ):
                    v52_acquire.acquire_sources(
                        root,
                        run_spec,
                        years=range(2018, 2025),
                        mode="hybrid",
                        resume=False,
                        stop_after=None,
                        legacy_root=root / LEGACY_ROOT,
                        protected_inventory_path=inventory_path,
                        plan_only=True,
                    )

            self.assertEqual(validation.successful_validations, 0)
            self.assertEqual(validation.registry_contract_reads, 0)

    def test_stage_preregisters_every_peer_before_concurrent_network_gates(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _AcquisitionRequest,
            _execute_stage,
        )
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from tests.test_v52_acquire import _capture, _query

        first = _query()
        second = dataclasses.replace(
            first,
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        queries = (first, second)
        batch = SimpleNamespace(batch_id="b" * 64, queries=queries)
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=load_run_spec(ROOT / RUN_SPEC_RELATIVE),
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
        )
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            checkpoint = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(checkpoint)
            return checkpoint

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")

            def gate(*_args, **_kwargs) -> None:
                for query in queries:
                    progress = store.read_verified_query_progress(query)
                    self.assertEqual(progress.registered_query, query)

            result = _execute_stage(
                request,
                _AcquisitionPlan(
                    contract_sha256="a" * 64,
                    query_batch_ids=(batch.batch_id,),
                    legacy_dispositions=("FETCH_REQUIRED",),
                ),
                store,
                stage="STATIC_SCHEDULES",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=None,
                fetcher=lambda query, **_kwargs: dataclasses.replace(
                    _capture(b"{}"), final_url=query.endpoint
                ),
                checkpoint_publisher=publish,
                parser=lambda _raw, _query: {},
                network_gate=gate,
            )

        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.network_attempts, 2)
        self.assertEqual(
            result.checkpoint.completed_query_ids,
            tuple(sorted(result.checkpoint.completed_query_ids)),
        )

    def test_source_derived_stage_threads_disposition_authority_and_checkpoint_id(
        self,
    ) -> None:
        ROOT = SOURCE_AUTHORITY_ROOT
        from contextlib import nullcontext

        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _AcquisitionRequest,
            _execute_stage,
        )
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            DuplicateGamePkDispositionAuthority,
            build_duplicate_disposition_authority,
        )
        from mlb_backtest.v52_evidence import (
            V52EvidenceStore,
            logical_query_id,
        )

        source_run = load_source_run_spec(
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=(
                ROOT
                / "protocols/v5_2_qd/"
                / "historical_walkforward_500_superseding_565534.json"
            ),
            acquisition_contract_path=(
                ROOT
                / "protocols/v5_2_qd/"
                / "historical_source_acquisition_superseding_565534.json"
            ),
        )
        self.assertIsInstance(authority, DuplicateGamePkDispositionAuthority)
        query_id = logical_query_id(authority.query)
        self.assertEqual(
            query_id,
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
        )
        batch = SimpleNamespace(batch_id="b" * 64, queries=(authority.query,))
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=source_run,
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
        )
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            checkpoint = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(checkpoint)
            return checkpoint

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            pending = SimpleNamespace(status="PENDING")
            success = SimpleNamespace(status="SUCCESS")
            with (
                patch(
                    "mlb_backtest.v52_acquire._source_disposition_authorities",
                    return_value={query_id: isolated},
                ),
                patch.object(
                    store,
                    "read_verified_query_progress",
                    side_effect=(pending, pending, success, success),
                ) as progress,
                patch.object(
                    store, "acquire_query", return_value=nullcontext()
                ),
                patch.object(
                    store,
                    "publish_derived_disposition",
                    return_value=SimpleNamespace(query_id=query_id),
                ) as derived_writer,
            ):
                result = _execute_stage(
                    request,
                    _AcquisitionPlan(
                        contract_sha256="a" * 64,
                        query_batch_ids=(batch.batch_id,),
                        legacy_dispositions=("FETCH_REQUIRED",),
                    ),
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail(
                        "derived disposition must not fetch"
                    ),
                    checkpoint_publisher=publish,
                    parser=lambda _raw, _query: {},
                )

        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0].dispositioned_query_ids, ())
        self.assertEqual(checkpoints[1].dispositioned_query_ids, (query_id,))
        self.assertEqual(result.checkpoint.dispositioned_query_ids, (query_id,))
        self.assertEqual(len(progress.call_args_list), 4)
        self.assertTrue(
            all(
                call.kwargs.get("authority") is isolated
                for call in progress.call_args_list
            )
        )
        derived_writer.assert_called_once()
        self.assertIs(derived_writer.call_args.kwargs["authority"], isolated)

    def test_source_real_run_stages_threads_authenticated_paths_and_stops_before_network_without_task7(
        self,
    ) -> None:
        ROOT = SOURCE_AUTHORITY_ROOT
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _AcquisitionRequest,
            _real_run_stages,
        )

        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)
        fetcher = Mock(side_effect=AssertionError("network must remain closed"))
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=source_run,
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
            fetcher=fetcher,
            acquisition_contract_path=source_contract_path,
        )
        plan = _AcquisitionPlan(
            contract_sha256="a" * 64,
            query_batch_ids=("b" * 64,),
            legacy_dispositions=(),
        )
        source_raw = ROOT / source_run.raw["namespaces"]["raw"]
        static = SimpleNamespace(batch_id="b" * 64, queries=())
        latest = SimpleNamespace(
            stage="STATIC_SCHEDULES",
            receipt_sha256="c" * 64,
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        inventory = SimpleNamespace(
            path=(
                ROOT
                / source_run.raw["namespaces"]["audit"]
                / "acquisition/schedule_anchor_inventory.json"
            ),
            sha256="d" * 64,
        )

        def build_static(run_spec_path: Path, acquisition_contract_path: Path):
            self.assertEqual(run_spec_path, source_run_path)
            self.assertEqual(acquisition_contract_path, source_contract_path)
            return static

        def publish_inventory(root: Path, authority, **kwargs):
            self.assertEqual(root, ROOT)
            self.assertIs(authority, source_run)
            self.assertEqual(
                kwargs,
                {
                    "acquisition_contract_path": source_contract_path,
                    "static_batch_id": static.batch_id,
                    "evidence_root": source_raw,
                },
            )
            return inventory

        def build_reference(
            root: Path,
            run_spec_path: Path,
            *,
            acquisition_contract_path: Path,
            static_batch_id: str,
            evidence_root: Path,
        ):
            self.assertEqual(root, ROOT)
            self.assertEqual(run_spec_path, source_run_path)
            self.assertEqual(acquisition_contract_path, source_contract_path)
            self.assertEqual(static_batch_id, static.batch_id)
            self.assertEqual(evidence_root, source_raw)
            raise TrustIntegrityError(
                "Task 7 derived disposition batch is not available"
            )

        with (
            patch("mlb_backtest.v52_acquire._real_authenticate", return_value=plan),
            patch("mlb_backtest.v52_acquire._current_runner_guard"),
            patch(
                "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                return_value=source_raw,
            ),
            patch("mlb_backtest.v52_acquire.V52EvidenceStore"),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                return_value=None,
            ),
            patch(
                "mlb_backtest.v52_query_plan.build_static_batch",
                side_effect=build_static,
            ),
            patch("mlb_backtest.v52_query_plan.publish_query_batch"),
            patch(
                "mlb_backtest.v52_acquire._execute_stage",
                return_value=SimpleNamespace(
                    checkpoint=latest,
                    fetched=0,
                    resumed=0,
                    network_attempts=0,
                ),
            ),
            patch(
                "mlb_backtest.v52_schedule_inventory.publish_schedule_anchor_inventory",
                side_effect=publish_inventory,
            ),
            patch("mlb_backtest.v52_acquire._validate_schedule_inventory_schema"),
            patch(
                "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                side_effect=build_reference,
            ),
        ):
            with self.assertRaisesRegex(
                TrustIntegrityError, "reference query batch integrity verification"
            ):
                _real_run_stages(request, plan)
        fetcher.assert_not_called()

    def test_source_terminal_replay_rejects_missing_disposition_before_opening_raw_store(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _AcquisitionRequest,
            _real_replay_terminal,
        )

        source_run = load_source_run_spec(
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        plan = _AcquisitionPlan(
            contract_sha256="a" * 64,
            query_batch_ids=("1" * 64, "2" * 64, "3" * 64),
            legacy_dispositions=("PROMOTED", *("FETCH_REQUIRED",) * 47),
        )
        checkpoint = SimpleNamespace(
            stage="REMAINING",
            query_batch_ids=("1" * 64, "2" * 64, "3" * 64, "4" * 64),
            completed_query_ids=("8" * 64,),
            pending_query_ids=(),
            quarantined_query_ids=(),
            stage_evidence_receipt_sha256s=tuple(f"{index:064x}" for index in range(10)),
            dispositioned_query_ids=(),
        )
        inventory = SimpleNamespace(
            payload={"protected_inventory": {"sha256": "5" * 64}}
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw_root = root / source_run.raw["namespaces"]["raw"]
            raw_root.mkdir(parents=True)
            request = _AcquisitionRequest(
                root=root,
                run_spec=source_run,
                years=range(2018, 2025),
                mode="hybrid",
                resume=True,
                stop_after=None,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=root / INVENTORY_PATH,
                reserve_bytes=20 * 1024**3,
                plan_only=False,
            )
            with (
                patch(
                    "mlb_backtest.execution_registry.load_terminal_acquisition_checkpoint",
                    return_value=checkpoint,
                ),
                patch(
                    "mlb_backtest.v52_query_plan.load_query_batch",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "mlb_backtest.v52_legacy_decision_inventory.load_verified_legacy_decision_inventory",
                    return_value=inventory,
                ),
                patch(
                    "mlb_backtest.v52_legacy.load_protected_legacy_catalog",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "mlb_backtest.v52_acquire._validate_remaining_decisions",
                    return_value=((SimpleNamespace(), SimpleNamespace()),),
                ),
                patch(
                    "mlb_backtest.v52_acquire._validate_terminal_remaining_promotion"
                ),
                patch(
                    "mlb_backtest.v52_acquire.V52EvidenceStore.open_read_only"
                ) as open_store,
            ):
                with self.assertRaisesRegex(
                    TrustIntegrityError, "disposition|terminal.*authority"
                ):
                    _real_replay_terminal(request, plan)
        open_store.assert_not_called()

    def test_source_timezone_checkpoint_writer_uses_disposition_and_new_audit_paths(
        self,
    ) -> None:
        ROOT = SOURCE_AUTHORITY_ROOT
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _AcquisitionRequest,
            _real_run_stages,
        )
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )
        from mlb_backtest.v52_evidence import logical_query_id

        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=source_run_path,
            acquisition_contract_path=source_contract_path,
        )
        query_id = logical_query_id(authority.query)
        static = SimpleNamespace(batch_id="1" * 64, queries=(authority.query,))
        inventory = SimpleNamespace(
            path=(
                ROOT
                / source_run.raw["namespaces"]["audit"]
                / "acquisition/schedule_anchor_inventory.json"
            ),
            sha256="2" * 64,
        )
        reference = SimpleNamespace(
            batch_id="3" * 64,
            queries=(),
            derived_from_sha256=inventory.sha256,
        )
        static_checkpoint = SimpleNamespace(
            stage="STATIC_SCHEDULES",
            receipt_sha256="4" * 64,
            query_batch_ids=(static.batch_id,),
            completed_query_ids=(query_id,),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        reference_checkpoint = SimpleNamespace(
            stage="REFERENCE_INPUTS",
            receipt_sha256="5" * 64,
            query_batch_ids=(static.batch_id, reference.batch_id),
            completed_query_ids=(query_id,),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=source_run,
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
            acquisition_contract_path=source_contract_path,
        )
        plan = _AcquisitionPlan(
            contract_sha256="6" * 64,
            query_batch_ids=(static.batch_id,),
            legacy_dispositions=(),
        )
        audit_root = ROOT / source_run.raw["namespaces"]["audit"]

        def capture_timezone(
            root: Path, run_spec, **values
        ) -> SimpleNamespace:
            self.assertEqual(root, ROOT)
            self.assertIs(run_spec, source_run)
            self.assertEqual(values["stage"], "TIMEZONE_REVIEW")
            self.assertEqual(values["dispositioned_query_ids"], (query_id,))
            self.assertEqual(
                values["stage_evidence_receipts"],
                (
                    inventory.path,
                    audit_root / "acquisition/timezone_reviews/coverage.json",
                    audit_root / "acquisition/timezone_reviews/semantic.json",
                    audit_root / "acquisition/venue_timezone_candidate.json",
                    audit_root / "acquisition/venue_timezone_seal.json",
                    ROOT / "config/v5_2_qd/venue_timezones.json",
                ),
            )
            raise TrustIntegrityError("stop after timezone checkpoint assertion")

        checkpoint_scope_calls: list[tuple[Path, object, object | None]] = []

        @contextmanager
        def sealed_checkpoint_scope(
            root: Path, run_spec, *, legacy_evidence=None
        ):
            checkpoint_scope_calls.append((root, run_spec, legacy_evidence))
            yield

        with (
            patch("mlb_backtest.v52_acquire._real_authenticate", return_value=plan),
            patch("mlb_backtest.v52_acquire._current_runner_guard"),
            patch(
                "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                return_value=ROOT / source_run.raw["namespaces"]["raw"],
            ),
            patch("mlb_backtest.v52_acquire.V52EvidenceStore"),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                return_value=None,
            ),
            patch("mlb_backtest.v52_query_plan.build_static_batch", return_value=static),
            patch("mlb_backtest.v52_query_plan.publish_query_batch"),
            patch(
                "mlb_backtest.v52_acquire._execute_stage",
                side_effect=(
                    SimpleNamespace(
                        checkpoint=static_checkpoint,
                        fetched=0,
                        resumed=0,
                        network_attempts=0,
                    ),
                    SimpleNamespace(
                        checkpoint=reference_checkpoint,
                        fetched=0,
                        resumed=0,
                        network_attempts=0,
                    ),
                ),
            ),
            patch(
                "mlb_backtest.v52_schedule_inventory.publish_schedule_anchor_inventory",
                return_value=inventory,
            ),
            patch("mlb_backtest.v52_acquire._validate_schedule_inventory_schema"),
            patch(
                "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                return_value=reference,
            ),
            patch("mlb_backtest.v52_timezone_review.publish_timezone_candidate"),
            patch("mlb_backtest.v52_acquire._load_required_timezone_gate"),
            patch(
                "mlb_backtest.v52_acquire._source_disposition_authorities",
                return_value={query_id: authority},
            ),
            patch(
                "mlb_backtest.execution_registry._using_verified_source_checkpoint",
                side_effect=sealed_checkpoint_scope,
            ) as checkpoint_scope,
            patch(
                "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                side_effect=capture_timezone,
            ),
        ):
            with self.assertRaisesRegex(
                TrustIntegrityError, "timezone checkpoint integrity verification"
            ):
                _real_run_stages(request, plan)
        checkpoint_scope.assert_called_once_with(
            ROOT,
            source_run,
            legacy_evidence=None,
        )
        self.assertEqual(checkpoint_scope_calls, [(ROOT, source_run, None)])

    def test_source_remaining_checkpoint_writer_uses_disposition_and_new_audit_paths(
        self,
    ) -> None:
        ROOT = SOURCE_AUTHORITY_ROOT
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _AcquisitionRequest,
            _run_remaining_stage,
        )
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import (
            parse_protected_raw_inventory_document,
        )

        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            / "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=source_run_path,
            acquisition_contract_path=source_contract_path,
        )
        query_id = logical_query_id(authority.query)
        prefix = (
            SimpleNamespace(batch_id="1" * 64, queries=(authority.query,)),
            SimpleNamespace(batch_id="2" * 64, queries=()),
            SimpleNamespace(batch_id="3" * 64, queries=()),
        )
        partition = SimpleNamespace(batch_id="4" * 64, queries=())
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=source_run,
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
            acquisition_contract_path=source_contract_path,
        )
        legacy_fixture_parent = Path(tempfile.mkdtemp(prefix="v52-source-legacy-"))
        self.addCleanup(shutil.rmtree, legacy_fixture_parent)
        legacy_fixture_root, legacy_inventory_path, legacy_inventory_sha256 = (
            _legacy_decision_repository(legacy_fixture_parent)
        )
        legacy_inventory_bytes = legacy_inventory_path.read_bytes()
        for lock_name in (
            ".baseball_savant_statcast.lock",
            ".mlb_schedule.lock",
            ".mlb_stats_pitcher_bf.lock",
            ".mlb_stats_pitching.lock",
        ):
            (legacy_fixture_root / LEGACY_ROOT / lock_name).write_bytes(b"")
        legacy_evidence = _bind_external_inventory(
            ROOT,
            legacy_fixture_root / LEGACY_ROOT,
            parse_protected_raw_inventory_document(legacy_inventory_bytes),
            legacy_inventory_sha256,
        )
        self.addCleanup(legacy_evidence.close)
        plan = _AcquisitionPlan(
            contract_sha256="5" * 64,
            query_batch_ids=tuple(batch.batch_id for batch in prefix),
            legacy_dispositions=(),
            legacy_evidence=legacy_evidence,
        )
        latest = SimpleNamespace(
            stage="PARTICIPANT_DERIVED",
            receipt_sha256="6" * 64,
            query_batch_ids=tuple(batch.batch_id for batch in prefix),
            completed_query_ids=(query_id,),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        audit_root = ROOT / source_run.raw["namespaces"]["audit"]
        participant_evidence = tuple(
            audit_root / f"acquisition/participant-{index}.json"
            for index in range(7)
        )
        expected_remaining_evidence = tuple(
            sorted(
                (
                    *participant_evidence,
                    ROOT / INVENTORY_PATH,
                    audit_root / "acquisition/legacy_decision_inventory.json",
                    audit_root
                    / "acquisition/query_batches"
                    / f"{prefix[1].batch_id}.json",
                ),
                key=str,
            )
        )
        inventory = SimpleNamespace(
            payload={
                "protected_inventory": {"sha256": legacy_inventory_sha256}
            }
        )

        def capture_remaining(
            root: Path, run_spec, **values
        ) -> SimpleNamespace:
            self.assertEqual(root, ROOT)
            self.assertIs(run_spec, source_run)
            self.assertEqual(values["stage"], "REMAINING")
            self.assertEqual(values["dispositioned_query_ids"], (query_id,))
            self.assertEqual(
                values["query_batch_ids"],
                (*tuple(batch.batch_id for batch in prefix), partition.batch_id),
            )
            self.assertEqual(
                values["stage_evidence_receipts"], expected_remaining_evidence
            )
            self.assertEqual(values["split_receipt_paths"], ())
            raise TrustIntegrityError("stop after REMAINING checkpoint assertion")

        with (
            patch(
                "mlb_backtest.v52_query_plan.build_statcast_root_batch_for_authority",
                return_value=prefix[1],
            ) as statcast_builder,
            patch(
                "mlb_backtest.v52_query_plan._publish_query_batch_for_authority"
            ) as publish_batch,
            patch(
                "mlb_backtest.v52_legacy_decision_inventory.publish_legacy_decision_inventory",
                return_value=inventory,
            ) as inventory_publisher,
            patch(
                "mlb_backtest.v52_query_plan.build_legacy_partition_batch",
                return_value=partition,
            ) as partition_builder,
            patch(
                "mlb_backtest.v52_legacy.load_protected_legacy_catalog",
                return_value=SimpleNamespace(),
            ),
            patch(
                "mlb_backtest.v52_acquire._validate_remaining_decisions",
                return_value=(),
            ),
            patch(
                "mlb_backtest.v52_acquire._source_disposition_authorities",
                return_value={query_id: authority},
            ),
            patch(
                "mlb_backtest.v52_acquire._remaining_populations",
                return_value=((query_id,), (), (), ()),
            ),
            patch(
                "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                side_effect=capture_remaining,
            ),
        ):
            with self.assertRaisesRegex(
                TrustIntegrityError, "REMAINING checkpoint integrity verification"
            ):
                _run_remaining_stage(
                    request,
                    plan,
                    SimpleNamespace(),
                    fixed_prefix=prefix,
                    participant_evidence=participant_evidence,
                    latest=latest,
                    evidence_root=ROOT / source_run.raw["namespaces"]["raw"],
                    runner_guard=SimpleNamespace(),
                    disk_budget=SimpleNamespace(),
                )
            statcast_builder.assert_called_once_with(
                ROOT,
                run_spec_path=source_run_path,
                acquisition_contract_path=source_contract_path,
            )
            self.assertEqual(
                publish_batch.call_args_list,
                [
                    call(ROOT, prefix[1], legacy_evidence=legacy_evidence),
                    call(ROOT, partition, legacy_evidence=legacy_evidence),
                ],
            )
            inventory_publisher.assert_called_once_with(
                ROOT,
                run_spec_path=source_run_path,
                acquisition_contract_path=source_contract_path,
                legacy_evidence=legacy_evidence,
            )
            partition_builder.assert_called_once_with(
                ROOT,
                run_spec_path=source_run_path,
                acquisition_contract_path=source_contract_path,
                legacy_evidence=legacy_evidence,
            )
            with self.assertRaisesRegex(
                TrustIntegrityError, "REMAINING legacy evidence is unavailable"
            ):
                _run_remaining_stage(
                    request,
                    dataclasses.replace(plan, legacy_evidence=None),
                    SimpleNamespace(),
                    fixed_prefix=prefix,
                    participant_evidence=participant_evidence,
                    latest=latest,
                    evidence_root=ROOT / source_run.raw["namespaces"]["raw"],
                    runner_guard=SimpleNamespace(),
                    disk_budget=SimpleNamespace(),
                )
            with self.assertRaisesRegex(TypeError, "legacy evidence capability"):
                _run_remaining_stage(
                    request,
                    dataclasses.replace(plan, legacy_evidence=SimpleNamespace()),
                    SimpleNamespace(),
                    fixed_prefix=prefix,
                    participant_evidence=participant_evidence,
                    latest=latest,
                    evidence_root=ROOT / source_run.raw["namespaces"]["raw"],
                    runner_guard=SimpleNamespace(),
                    disk_budget=SimpleNamespace(),
                )

    def test_remaining_wave_preregisters_every_peer_before_network_gates(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _AcquisitionRequest,
            _run_remaining_wave,
        )
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from tests.test_v52_acquire import _capture, _query

        first = _query()
        second = dataclasses.replace(
            first,
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        queries = (first, second)
        batch = SimpleNamespace(batch_id="b" * 64, queries=queries)
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=load_run_spec(ROOT / RUN_SPEC_RELATIVE),
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")

            def gate(*_args, **_kwargs) -> None:
                for query in queries:
                    progress = store.read_verified_query_progress(query)
                    self.assertEqual(progress.registered_query, query)

            result = _run_remaining_wave(
                request,
                _AcquisitionPlan(
                    contract_sha256="a" * 64,
                    query_batch_ids=(batch.batch_id,),
                    legacy_dispositions=("FETCH_REQUIRED",),
                ),
                store,
                batches=(batch,),
                active_queries=queries,
                fetcher=lambda query, **_kwargs: dataclasses.replace(
                    _capture(b"{}"), final_url=query.endpoint
                ),
                parser=lambda _raw, _query: {},
                network_gate=gate,
            )

        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.network_attempts, 2)
        self.assertEqual(result.resumed, 0)
        self.assertEqual(result.splits, ())

    def test_plan_authentication_invokes_each_real_head_verifier_once(self) -> None:
        from mlb_backtest.v52_acquire import acquire_sources

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, _digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            arguments = dict(
                years=range(2018, 2025),
                mode="hybrid",
                resume=False,
                stop_after=None,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=inventory_path,
                plan_only=True,
            )
            with _authenticated_fixture_acquisition(root, _digest):
                acquire_sources(root, run_spec, **arguments)
            first_calls = tuple(self.head_blob_original_calls)
            self.assertGreaterEqual(len(first_calls), 1)
            self.assertEqual(len(first_calls), len(set(first_calls)))
            with _authenticated_fixture_acquisition(root, _digest):
                acquire_sources(root, run_spec, **arguments)
            self.assertEqual(tuple(self.head_blob_original_calls), first_calls)

    def test_memoized_batch_loader_separates_read_only_mode(self) -> None:
        from mlb_backtest import v52_query_plan

        writable = object()
        read_only = object()
        original = Mock(side_effect=(writable, read_only))
        batch_id = "a" * 64

        with self.memoized_batch_loader(loader=original):
            self.assertIs(
                v52_query_plan.load_query_batch(ROOT, batch_id), writable
            )
            self.assertIs(
                v52_query_plan.load_query_batch(ROOT, batch_id), writable
            )
            self.assertIs(
                v52_query_plan.load_query_batch(
                    ROOT,
                    batch_id,
                    read_only_progress=True,
                ),
                read_only,
            )
            self.assertIs(
                v52_query_plan.load_query_batch(
                    ROOT,
                    batch_id,
                    read_only_progress=True,
                ),
                read_only,
            )

        self.assertEqual(
            original.call_args_list,
            [
                call(ROOT, batch_id, read_only_progress=False),
                call(ROOT, batch_id, read_only_progress=True),
            ],
        )

    def test_preview_and_sealed_partition_ids_are_distinct_authorities(self) -> None:
        from mlb_backtest.v52_acquire import acquire_sources
        from mlb_backtest.v52_legacy_decision_inventory import (
            load_verified_legacy_decision_inventory,
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            with (
                _authenticated_fixture_acquisition(root, digest),
                _verified_legacy_decision_authority(root, digest),
            ):
                preview = acquire_sources(
                    root,
                    run_spec,
                    years=range(2018, 2025),
                    mode="hybrid",
                    resume=False,
                    stop_after=None,
                    legacy_root=root / LEGACY_ROOT,
                    protected_inventory_path=inventory_path,
                    plan_only=True,
                )
                sealed_inventory = publish_legacy_decision_inventory(root)
                self.assertEqual(
                    load_verified_legacy_decision_inventory(root),
                    sealed_inventory,
                )
                sealed_partition = build_legacy_partition_batch(root)
            self.assertNotEqual(
                preview.query_batch_sha256s[2],
                sealed_partition.batch_id,
            )
            self.assertEqual(
                sealed_partition.derived_from_sha256, sealed_inventory.sha256
            )

    def test_real_legacy_publication_requires_verified_fixture_authority(self) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            load_verified_legacy_decision_inventory,
            publish_legacy_decision_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _fixture_repository(Path(directory))

            # The acquisition contract seam alone must remain fail-closed at the
            # real legacy publisher's contract snapshot boundary.
            with (
                _authenticated_fixture_acquisition(root, digest),
                self.assertRaisesRegex(ValueError, "protected inventory hash differs"),
            ):
                publish_legacy_decision_inventory(root)

            with (
                _authenticated_fixture_acquisition(root, digest),
                _verified_legacy_decision_authority(root, digest),
            ):
                published = publish_legacy_decision_inventory(root)
                loaded = load_verified_legacy_decision_inventory(root)

            self.assertEqual(loaded, published)
            self.assertEqual(
                published.sha256,
                hashlib.sha256(published.path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                published.payload["protected_inventory"]["sha256"], digest
            )

            with (
                _authenticated_fixture_acquisition(root, digest),
                _verified_legacy_decision_authority(root, "0" * 64),
                self.assertRaisesRegex(ValueError, "protected inventory hash differs"),
            ):
                publish_legacy_decision_inventory(root)

    def test_sources_ready_replays_terminal_but_later_phases_fail_trust(self) -> None:
        from contextlib import contextmanager

        from mlb_backtest.v52_acquire import (
            AcquisitionSummary,
            TrustIntegrityError,
            _AcquisitionPlan,
            _AcquisitionRequest,
            _AcquireOps,
            _RuntimeState,
            _run_acquisition,
        )

        summary = AcquisitionSummary(
            contract_sha256="a" * 64,
            query_batch_sha256s=("b" * 64,),
            promoted=0,
            fetched=0,
            planned_promoted=1,
            planned_fetch_required=47,
            resumed=48,
            quarantined=0,
            pending=0,
            network_attempts=0,
        )
        request = _AcquisitionRequest(
            root=ROOT,
            run_spec=load_run_spec(ROOT / RUN_SPEC_RELATIVE),
            years=range(2018, 2025),
            mode="hybrid",
            resume=True,
            stop_after=None,
            legacy_root=ROOT / LEGACY_ROOT,
            protected_inventory_path=ROOT / INVENTORY_PATH,
            reserve_bytes=20 * 1024**3,
            plan_only=False,
        )

        @contextmanager
        def lock(_request):
            self.fail("terminal phase must not acquire the runner lock")
            yield

        def ops(phase: str) -> _AcquireOps:
            return _AcquireOps(
                authenticate=lambda _request: _AcquisitionPlan(
                    "a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)
                ),
                runner_lock=lock,
                inspect_runtime=lambda _request, _plan: _RuntimeState(phase, True),
                begin_runtime=lambda *_args: self.fail(
                    "terminal replay must not mutate registry"
                ),
                replay_terminal=lambda *_args: summary,
                run_stages=lambda *_args: self.fail(
                    "SOURCES_READY must not call mutation-capable stages"
                ),
            )

        mutation_capable = _AcquireOps(
            authenticate=lambda _request: _AcquisitionPlan(
                "a" * 64,
                ("b" * 64,),
                ("FETCH_REQUIRED",),
            ),
            runner_lock=lock,
            inspect_runtime=lambda _request, _plan: _RuntimeState(
                "SOURCES_READY", True
            ),
            begin_runtime=lambda *_args: self.fail("runtime mutation reached"),
            run_stages=lambda *_args: summary,
        )
        with self.assertRaisesRegex(TrustIntegrityError, "read-only terminal"):
            _run_acquisition(request, mutation_capable)

        for resume in (False, True):
            selected = dataclasses.replace(request, resume=resume)
            with self.subTest(phase="SOURCES_READY", resume=resume):
                self.assertEqual(
                    _run_acquisition(selected, ops("SOURCES_READY")), summary
                )
            for phase in (
                "OOF_SEALED",
                "CARDS_SEALED",
                "TARGET_LOCKED",
                "OUTCOMES_JOINED",
                "SAMPLE_500_SEALED",
                "VERIFIED",
            ):
                with (
                    self.subTest(phase=phase, resume=resume),
                    self.assertRaises(TrustIntegrityError),
                ):
                    _run_acquisition(selected, ops(phase))

    def test_main_uses_authenticated_phase_exit_matrix_for_both_resume_values(
        self,
    ) -> None:
        from contextlib import contextmanager

        from mlb_backtest import v52_acquire
        from mlb_backtest.v52_acquire import AcquisitionSummary, _RuntimeState

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, _digest = _fixture_repository(Path(directory))
            base = [
                "--root",
                str(root),
                "--run-spec",
                str(root / RUN_SPEC_RELATIVE),
                "--years",
                "2018:2024",
                "--mode",
                "hybrid",
                "--legacy-root",
                str(root / LEGACY_ROOT),
                "--protected-inventory",
                str(inventory_path),
            ]
            summary = AcquisitionSummary(
                contract_sha256="a" * 64,
                query_batch_sha256s=("b" * 64,) * 4,
                promoted=0,
                fetched=0,
                planned_promoted=1,
                planned_fetch_required=47,
                resumed=81,
                quarantined=0,
                pending=0,
                network_attempts=0,
            )

            @contextmanager
            def lock(_request):
                self.fail("terminal phase must not acquire the runner lock")
                yield

            for resume in (False, True):
                argv = [*base, *(["--resume"] if resume else [])]
                for phase, expected in (("SOURCES_READY", 0), ("VERIFIED", 2)):
                    ops = dataclasses.replace(
                        v52_acquire._REAL_OPS,
                        runner_lock=lock,
                        inspect_runtime=lambda _request, _plan, phase=phase: (
                            _RuntimeState(phase, True)
                        ),
                        begin_runtime=lambda *_args: self.fail(
                            "authenticated terminal phase must not begin runtime"
                        ),
                        replay_terminal=lambda *_args: summary,
                        run_stages=lambda *_args: self.fail(
                            "terminal phase must not call stage runner"
                        ),
                    )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        self.subTest(phase=phase, resume=resume),
                        _authenticated_fixture_acquisition(root, _digest),
                        patch("mlb_backtest.v52_acquire._REAL_OPS", ops),
                        patch("sys.stdout", stdout),
                        patch("sys.stderr", stderr),
                    ):
                        self.assertEqual(v52_acquire.main(argv), expected)
                    if expected == 0:
                        self.assertEqual(stderr.getvalue(), "")
                        self.assertEqual(json.loads(stdout.getvalue())["resumed"], 81)
                    else:
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertIn("later than acquisition", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
