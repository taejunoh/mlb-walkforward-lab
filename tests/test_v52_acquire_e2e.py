from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from mlb_backtest.protocols import load_run_spec
from mlb_backtest.v52_evidence import AttemptCapture, LogicalQuery
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from tests.test_v52_legacy_decision_inventory import (
    INVENTORY_PATH,
    LEGACY_ROOT,
    _legacy_decision_repository,
)
from tests.test_v52_source_schedule import _boxscore_raw


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
PROTECTED_DUPLICATES = (
    ROOT / "mlb_backtest/v52_query_plan 2.py",
    ROOT / "reports/v5_2_qd/walkforward_500_preflight 2.md",
    ROOT / "schemas/v5_2_preflight.schema 2.json",
    ROOT / "tests/test_v52_query_plan 2.py",
)


def _canonical_file(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _snapshot_tree(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _protected_duplicate_snapshot() -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_ino) for path in PROTECTED_DUPLICATES
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


def _fixture_repository(parent: Path) -> tuple[Path, Path]:
    root, inventory_path, _digest = _legacy_decision_repository(
        parent, production_shaped=True
    )
    _bind_runtime(root)
    return root, inventory_path


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

        self.assertEqual(len(self.bound_schema_paths), 14)
        self.assertEqual(
            execution_registry._BOUND_EXECUTION_CODE,
            ("mlb_backtest/execution_registry.py", *self.bound_schema_paths),
        )
        self.assertTrue(
            {
                "schemas/v5_2_clean_source_replay_receipt.schema.json",
                "schemas/v5_2_source_receipt_commit.schema.json",
                "schemas/v5_2_source_review_receipt.schema.json",
            }
            <= set(self.bound_schema_paths)
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
            root, inventory_path = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
            fetcher = DeterministicFetcher()
            with self.memoized_batch_loader():
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

            with self.memoized_batch_loader():
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
                "checkpoint|evidence|registry|authentication|authority",
            ):
                _acquire(root, inventory_path, fetcher)
            self.assertEqual(_snapshot_tree(root), tampered_tree)
            self.assertEqual(len(fetcher.query_ids), 73)

        self.assertEqual(_protected_duplicate_snapshot(), duplicate_before)

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
            root, inventory_path = _fixture_repository(Path(directory))
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
            acquire_sources(root, run_spec, **arguments)
            first_calls = tuple(self.head_blob_original_calls)
            self.assertGreaterEqual(len(first_calls), 1)
            self.assertEqual(len(first_calls), len(set(first_calls)))
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
            root, inventory_path = _fixture_repository(Path(directory))
            run_spec = load_run_spec(root / RUN_SPEC_RELATIVE)
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
            root, inventory_path = _fixture_repository(Path(directory))
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
