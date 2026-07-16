from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.execution_registry import AcquisitionCheckpoint
from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    V52EvidenceStore,
    logical_query_id,
)
from mlb_backtest.v52_query_plan import QueryBatch, SplitReceipt
from mlb_backtest.v52_source_contracts import recompute_query_sidecar
from tests.test_v52_source_schedule import _query, _schedule_raw


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "mlb_backtest/v52_verify_sources.py"
SOURCE_FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
_VENUE_GUARD_PAYLOAD = json.loads((SOURCE_FIXTURES / "venues_mini.json").read_bytes())
_VENUE_GUARD_PAYLOAD["venues"] = _VENUE_GUARD_PAYLOAD["venues"][:1]
PARSER_IMPORT_GUARD_CASES = (
    "schedule",
    "statcast",
    "d1_pitching",
    "career_bf",
    "team_venue_reference",
)
PARSER_IMPORT_GUARD_SCRIPT = r"""
import importlib.abc
import sys

BLOCKED = (
    "aiohttp",
    "http.client",
    "httpx",
    "mlb_backtest.sources",
    "mlb_backtest.v52_http",
    "requests",
    "urllib.error",
    "urllib.request",
)


class OfflineImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(
            fullname == blocked or fullname.startswith(blocked + ".")
            for blocked in BLOCKED
        ):
            raise ImportError("NETWORK_MODULE_BLOCKED:" + fullname)
        return None


sys.meta_path.insert(0, OfflineImportGuard())
if len(sys.argv) > 2 and sys.argv[2] == "preload":
    import types

    sys.modules["requests"] = types.ModuleType("requests")

preloaded = sorted(
    module_name
    for module_name, module in sys.modules.items()
    if module is not None
    and any(
        module_name == blocked or module_name.startswith(blocked + ".")
        for blocked in BLOCKED
    )
)
if preloaded:
    raise ImportError("NETWORK_MODULE_PRELOADED:" + ",".join(preloaded))

from mlb_backtest.v52_evidence import LogicalQuery
from mlb_backtest.v52_source_contracts import recompute_query_sidecar


case = sys.argv[1]
if case == "schedule":
    query = LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={
            "sportId": 1,
            "gameType": "R",
            "startDate": "2024-03-20",
            "endDate": "2024-09-30",
            "hydrate": "probablePitcher,lineups,venue,linescore",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-03-20",
            "end_date": "2024-09-30",
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    raw = __SCHEDULE_RAW__
    semantics = recompute_query_sidecar(query, raw)
    if semantics["game_pks"] != [9001, 9002]:
        raise AssertionError("schedule parser dispatch differs")
elif case == "statcast":
    query = LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={
            "all": "true",
            "type": "details",
            "player_type": "batter",
            "game_date_gt": "2024-04-01",
            "game_date_lt": "2024-04-30",
            "hfGT": "R|",
            "group_by": "name-year",
            "sort_col": "pitches",
            "player_event_sort": "api_p_release_speed",
            "sort_order": "desc",
            "min_pitches": "0",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-04-01",
            "end_date": "2024-04-30",
        },
        expected_media_type="text/csv",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, __STATCAST_RAW__)
    if semantics["game_pks"] != [9001]:
        raise AssertionError("Statcast parser dispatch differs")
elif case == "d1_pitching":
    query = LogicalQuery(
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint="https://statsapi.mlb.com/api/v1/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "sportIds": 1,
            "gameType": "R",
            "playerPool": "ALL",
            "limit": 5000,
            "startDate": "2024-03-20",
            "endDate": "2024-04-01",
        },
        requested_range={
            "season": 2024,
            "start_date": "2024-03-20",
            "as_of_date": "2024-04-01",
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": "2024-04-01"},
            "response_completeness": {
                "limit": 5000,
                "reject_total_splits_at_or_above_limit": True,
            },
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, __PITCHING_RAW__)
    if semantics["batters_faced"] != 75:
        raise AssertionError("D-1 pitching parser dispatch differs")
elif case == "career_bf":
    query = LogicalQuery(
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": "1876-01-01",
            "endDate": "2017-12-31",
        },
        requested_range={
            "pitcher_id": 101,
            "inclusive_end": "2017-12-31",
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": "2024-04-30"},
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, b'{"stats":[]}')
    if semantics["baseline_batters_faced"] != 0:
        raise AssertionError("career BF parser dispatch differs")
elif case == "team_venue_reference":
    common = {
        "source": "venue_team_timezone_table",
        "grain": "effective_dated_venue_team_reference",
        "expected_media_type": "application/json",
        "parser_version": "v5.2.1",
        "parent_query_id": None,
        "run_spec_sha256": "a" * 64,
        "acquisition_contract_sha256": "b" * 64,
    }
    team_query = LogicalQuery(
        **common,
        endpoint="https://statsapi.mlb.com/api/v1/teams",
        parameters={
            "sportId": 1,
            "season": 2024,
            "hydrate": "venue(location)",
        },
        requested_range={"season": 2024, "entity": "teams"},
    )
    venue_query = LogicalQuery(
        **common,
        endpoint="https://statsapi.mlb.com/api/v1/venues/31",
        parameters={"hydrate": "location"},
        requested_range={"venue_id": 31},
    )
    team_semantics = recompute_query_sidecar(team_query, __TEAM_RAW__)
    venue_semantics = recompute_query_sidecar(venue_query, __VENUE_RAW__)
    if [row["team_id"] for row in team_semantics["teams"]] != [10, 20]:
        raise AssertionError("team reference parser dispatch differs")
    if [row["venue_id"] for row in venue_semantics["venues"]] != [31]:
        raise AssertionError("venue reference parser dispatch differs")
else:
    raise AssertionError("unknown parser guard case: " + case)

for module_name, module in tuple(sys.modules.items()):
    if module is None or not module_name.startswith("mlb_backtest"):
        continue
    exposed = {
        name
        for name in vars(module)
        if name in {"fetch", "fetch_attempt", "fetch_bytes", "urlopen"}
        or name.startswith("fetch_")
    }
    if exposed:
        raise AssertionError(
            "NETWORK_SYMBOL_EXPOSED:"
            + module_name
            + ":"
            + ",".join(sorted(exposed))
        )
"""
for _placeholder, _raw in (
    ("__SCHEDULE_RAW__", _schedule_raw()),
    ("__STATCAST_RAW__", (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes()),
    ("__PITCHING_RAW__", (SOURCE_FIXTURES / "pitching_mini.json").read_bytes()),
    ("__TEAM_RAW__", (SOURCE_FIXTURES / "team_mini.json").read_bytes()),
    (
        "__VENUE_RAW__",
        json.dumps(_VENUE_GUARD_PAYLOAD, separators=(",", ":")).encode(),
    ),
):
    PARSER_IMPORT_GUARD_SCRIPT = PARSER_IMPORT_GUARD_SCRIPT.replace(
        _placeholder, repr(_raw)
    )


def _snapshot_tree(
    root: Path,
) -> dict[Path, tuple[int, int, int, int, bytes | None]]:
    snapshot = {}
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        snapshot[path.relative_to(root)] = (
            stat.S_IFMT(metadata.st_mode),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
        )
    return snapshot


def _owned_schedule_evidence(
    root: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, bytes]:
    raw = _schedule_raw()
    query = _query()
    semantics = recompute_query_sidecar(query, raw)
    store = V52EvidenceStore(root)
    capture = AttemptCapture(
        started_at_utc="2026-07-16T12:00:00.000000Z",
        ended_at_utc="2026-07-16T12:00:01.000000Z",
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
    return store, query, raw


def _forge_sidecar_semantics(root: Path, query: LogicalQuery) -> None:
    verified = V52EvidenceStore.open_read_only(root).read_verified_success(query)
    success_path = verified.published_evidence.success_receipt_path
    success = json.loads(success_path.read_bytes())
    sidecar_path = root / str(success["query_sidecar_path"])
    sidecar = json.loads(sidecar_path.read_bytes())
    sidecar["semantics"]["game_pks"] = [999999]
    sidecar_contents = canonical_json_bytes(sidecar) + b"\n"
    sidecar_sha256 = hashlib.sha256(sidecar_contents).hexdigest()
    replacement = sidecar_path.with_name(f"{sidecar_sha256}.json")
    replacement.write_bytes(sidecar_contents)
    sidecar_path.unlink()
    success["query_sidecar_path"] = replacement.relative_to(root).as_posix()
    success["query_sidecar_sha256"] = sidecar_sha256
    success_path.write_bytes(canonical_json_bytes(success) + b"\n")


def _graph_fixture(*, recursive: bool = False) -> SimpleNamespace:
    def query(label: str, parent: str | None = None) -> LogicalQuery:
        base = _query()
        return dataclasses.replace(
            base,
            parameters={**base.parameters, "fixture": label},
            requested_range={**base.requested_range, "fixture": label},
            parent_query_id=parent,
        )

    root, parent = query("root"), query("parent")
    parent_id = logical_query_id(parent)
    left, right = query("left", parent_id), query("right", parent_id)
    batches = [
        QueryBatch("1" * 64, "REMAINING", None, (root, parent)),
        QueryBatch("2" * 64, "REMAINING", "1" * 64, (left, right)),
    ]

    def receipt(index: int, parent_batch, parent_query, child_batch, depth):
        return SplitReceipt(
            str(5 + index) * 64,
            str(7 + index) * 64,
            "REMAINING",
            parent_batch.batch_id,
            logical_query_id(parent_query),
            "ROW_CAP",
            1,
            depth,
            child_batch.batch_id,
            tuple(logical_query_id(item) for item in child_batch.queries),
            "2026-07-16T12:00:00.000000Z",
        )

    receipts = [receipt(0, batches[0], parent, batches[1], 1)]
    completed = [root, left, right]
    if recursive:
        left_id = logical_query_id(left)
        grand = (query("grand-left", left_id), query("grand-right", left_id))
        batches.append(QueryBatch("3" * 64, "REMAINING", "2" * 64, grand))
        receipts.append(receipt(1, batches[1], left, batches[2], 2))
        completed = [root, right, *grand]
    checkpoint = AcquisitionCheckpoint(
        4,
        "a" * 64,
        "b" * 64,
        "REMAINING",
        "2026-07-16T12:00:00.000000Z",
        "c" * 40,
        "d" * 64,
        tuple(batch.batch_id for batch in batches),
        tuple("e" * 64 for _ in batches),
        (),
        (),
        tuple(
            item.receipt_sha256
            for item in sorted(receipts, key=lambda x: x.parent_query_id)
        ),
        tuple(sorted(logical_query_id(item) for item in completed)),
        (),
        (),
        tuple(sorted(item.parent_query_id for item in receipts)),
    )
    return SimpleNamespace(
        checkpoint=checkpoint,
        batches=tuple(batches),
        receipts=tuple(receipts),
        root=root,
        parent=parent,
        left=left,
        right=right,
        completed=tuple(completed),
    )


class V52VerifySourcesTest(unittest.TestCase):
    def test_completed_query_graph_classifies_root_and_split_leaves(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx = _graph_fixture()
        enumerate_graph = getattr(verifier, "_enumerate_completed_query_graph", None)
        self.assertIsNotNone(enumerate_graph, "missing graph enumerator")
        graph = enumerate_graph(fx.checkpoint, fx.batches, fx.receipts)
        self.assertIsInstance(graph, verifier._TerminalQueryGraph)
        self.assertIs(graph.checkpoint, fx.checkpoint)
        self.assertEqual(graph.query_batches, fx.batches)
        self.assertEqual(
            graph.split_receipts,
            tuple(
                {item.parent_query_id: item for item in fx.receipts}[parent_id]
                for parent_id in fx.checkpoint.split_query_ids
            ),
        )
        self.assertEqual(graph.split_parent_queries, (fx.parent,))
        self.assertEqual(
            graph.completed_queries, tuple(sorted(fx.completed, key=logical_query_id))
        )
        self.assertEqual(graph.root_queries, (fx.root,))
        self.assertEqual(
            graph.split_leaf_queries,
            tuple(sorted((fx.left, fx.right), key=logical_query_id)),
        )
        self.assertNotIn(fx.parent, graph.completed_queries)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            graph.completed_queries = ()

    def test_completed_query_graph_rejects_population_binding_and_cycles(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx = _graph_fixture()
        cp, (root_batch, child_batch), (receipt,) = (
            fx.checkpoint,
            fx.batches,
            fx.receipts,
        )
        duplicate_batch = dataclasses.replace(child_batch, queries=(fx.root, fx.right))
        duplicate_receipt = dataclasses.replace(
            receipt,
            child_query_ids=tuple(logical_query_id(q) for q in duplicate_batch.queries),
        )
        cyclic_root = dataclasses.replace(
            root_batch, derived_from_sha256=child_batch.batch_id
        )
        back = dataclasses.replace(
            receipt,
            split_receipt_id="9" * 64,
            receipt_sha256="f" * 64,
            parent_batch_id=child_batch.batch_id,
            parent_query_id=logical_query_id(fx.left),
            depth=2,
            child_batch_id=cyclic_root.batch_id,
            child_query_ids=tuple(logical_query_id(q) for q in cyclic_root.queries),
        )
        attacks = (
            (cp, (root_batch, duplicate_batch), (duplicate_receipt,)),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((*cp.completed_query_ids, logical_query_id(fx.parent)))
                    ),
                ),
                fx.batches,
                fx.receipts,
            ),
            (
                dataclasses.replace(
                    cp, completed_query_ids=cp.completed_query_ids[:-1]
                ),
                fx.batches,
                fx.receipts,
            ),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((*cp.completed_query_ids, "0" * 64))
                    ),
                ),
                fx.batches,
                fx.receipts,
            ),
            (cp, fx.batches, (dataclasses.replace(receipt, parent_batch_id="0" * 64),)),
            (
                cp,
                fx.batches,
                (
                    dataclasses.replace(
                        receipt,
                        child_query_ids=tuple(reversed(receipt.child_query_ids)),
                    ),
                ),
            ),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((logical_query_id(fx.root), logical_query_id(fx.right)))
                    ),
                    split_query_ids=tuple(
                        sorted((receipt.parent_query_id, back.parent_query_id))
                    ),
                    split_receipt_sha256s=tuple(
                        sorted((receipt.receipt_sha256, back.receipt_sha256))
                    ),
                ),
                (cyclic_root, child_batch),
                (receipt, back),
            ),
        )
        enumerate_graph = getattr(verifier, "_enumerate_completed_query_graph", None)
        self.assertIsNotNone(enumerate_graph, "missing graph enumerator")
        for index, arguments in enumerate(attacks):
            with self.subTest(index=index), self.assertRaises(ValueError):
                enumerate_graph(*arguments)

    def test_terminal_query_graph_loader_uses_only_sealed_read_only_artifacts(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx, root = _graph_fixture(recursive=True), Path("/sealed/repository")
        run_spec = SimpleNamespace(raw={"namespaces": {"raw": "evidence/raw"}})
        batch_index = {item.batch_id: item for item in fx.batches}
        receipt_index = {item.parent_query_id: item for item in fx.receipts}
        loader = getattr(verifier, "_load_terminal_query_graph", None)
        self.assertIsNotNone(loader, "missing terminal graph loader")
        with (
            patch.object(
                verifier,
                "load_source_verification_checkpoint",
                return_value=fx.checkpoint,
                create=True,
            ) as checkpoint_loader,
            patch.object(
                verifier,
                "load_query_batch",
                side_effect=lambda _, key, **kw: batch_index[key],
                create=True,
            ) as batch_loader,
            patch.object(
                verifier,
                "load_split_receipt",
                side_effect=lambda _, key, **kw: receipt_index[key],
                create=True,
            ) as receipt_loader,
            patch.object(Path, "glob", side_effect=AssertionError("must not scan")),
            patch.object(Path, "rglob", side_effect=AssertionError("must not scan")),
        ):
            graph = loader(root, run_spec)
        checkpoint_loader.assert_called_once_with(root, run_spec)
        self.assertEqual(
            batch_loader.call_args_list,
            [call(root, item.batch_id, read_only_progress=True) for item in fx.batches],
        )
        self.assertEqual(
            receipt_loader.call_args_list,
            [
                call(
                    root,
                    key,
                    evidence_root=root / "evidence/raw",
                    read_only_progress=True,
                )
                for key in fx.checkpoint.split_query_ids
            ],
        )
        self.assertEqual(
            graph.completed_queries, tuple(sorted(fx.completed, key=logical_query_id))
        )
        self.assertIs(graph.checkpoint, fx.checkpoint)
        self.assertEqual(graph.query_batches, fx.batches)
        self.assertEqual(
            graph.split_receipts,
            tuple(receipt_index[parent_id] for parent_id in fx.checkpoint.split_query_ids),
        )
        self.assertEqual(
            graph.split_parent_queries,
            tuple(sorted((fx.parent, fx.left), key=logical_query_id)),
        )

    def test_completed_query_recompute_is_query_id_sorted(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        queries, store = tuple(reversed(_graph_fixture().completed)), object()
        recompute = getattr(verifier, "_recompute_completed_queries", None)
        self.assertIsNotNone(recompute, "missing completed recompute helper")
        with patch.object(
            verifier,
            "_read_and_recompute_query",
            side_effect=lambda _, q: {"query_id": logical_query_id(q)},
        ) as reader:
            result = recompute(store, queries)
        ordered = tuple(sorted(queries, key=logical_query_id))
        self.assertEqual(
            reader.call_args_list, [call(store, query) for query in ordered]
        )
        self.assertEqual(
            result, tuple({"query_id": logical_query_id(query)} for query in ordered)
        )

    def test_derived_disposition_is_independently_recomputed_without_mutation(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from tests.test_v52_evidence import _derived_query, _derived_semantics

        with tempfile.TemporaryDirectory() as directory:
            root, query = Path(directory).resolve() / "raw", _derived_query()
            store = V52EvidenceStore(root)
            with store.acquire_query(query):
                store.publish_derived_disposition(query, _derived_semantics())
            read_only = V52EvidenceStore.open_read_only(root)
            expected_sidecar = dict(
                read_only.read_verified_success(query).query_sidecar
            )
            before = _snapshot_tree(root)
            with patch.object(
                verifier, "recompute_query_sidecar", wraps=recompute_query_sidecar
            ) as parser:
                result = verifier._read_and_recompute_query(read_only, query)
            parser.assert_called_once_with(query, b"")
            self.assertEqual(
                result,
                {
                    "query_id": logical_query_id(query),
                    "query_sidecar": expected_sidecar,
                    "raw_sha256": None,
                    "storage_mode": "DERIVED_DISPOSITION",
                },
            )
            self.assertEqual(_snapshot_tree(root), before)

    def test_verifier_import_graph_has_no_fetch_or_http_client(self) -> None:
        self.assertTrue(MODULE_PATH.is_file(), "source verifier is not implemented")
        tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
        imported_modules: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.add(node.module or "")
                imported_names.update(alias.name for alias in node.names)

        forbidden_modules = {
            "aiohttp",
            "http.client",
            "httpx",
            "mlb_backtest.sources",
            "mlb_backtest.v52_http",
            "requests",
            "urllib.error",
            "urllib.request",
        }
        self.assertFalse(
            any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for imported in imported_modules
                for forbidden in forbidden_modules
            ),
            "offline verifier must not import an HTTP client module",
        )
        self.assertNotIn("v52_http", imported_names)
        self.assertFalse(
            any("fetch" in name.lower() for name in imported_names),
            "offline verifier must not import a fetch function",
        )

        blocked = {name: None for name in forbidden_modules}
        with patch.dict(sys.modules, blocked):
            sys.modules.pop("mlb_backtest.v52_verify_sources", None)
            imported = importlib.import_module("mlb_backtest.v52_verify_sources")
        self.assertTrue(hasattr(imported, "_read_and_recompute_query"))

    def test_registered_parser_dispatch_imports_no_network_surface(self) -> None:
        for case in PARSER_IMPORT_GUARD_CASES:
            with self.subTest(case=case):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        PARSER_IMPORT_GUARD_SCRIPT,
                        case,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr or completed.stdout,
                )

    def test_parser_guard_rejects_preloaded_blocked_module(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                PARSER_IMPORT_GUARD_SCRIPT,
                "schedule",
                "preload",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NETWORK_MODULE_PRELOADED:requests", completed.stderr)

    def test_owned_fetched_query_is_reread_and_recomputed_without_mutation(
        self,
    ) -> None:
        from mlb_backtest.v52_verify_sources import _read_and_recompute_query

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            _, query, raw = _owned_schedule_evidence(root)
            read_only = V52EvidenceStore.open_read_only(root)
            verified = read_only.read_verified_success(query)
            expected = {
                "query_id": logical_query_id(query),
                "query_sidecar": dict(verified.query_sidecar),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "storage_mode": "FETCHED_RAW",
            }
            before = _snapshot_tree(root)

            first = _read_and_recompute_query(read_only, query)
            second = _read_and_recompute_query(read_only, query)

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertEqual(_snapshot_tree(root), before)

    def test_sidecar_forgery_and_raw_mutation_fail_closed_without_cleanup(
        self,
    ) -> None:
        from mlb_backtest.v52_verify_sources import _read_and_recompute_query

        for mutation in ("sidecar", "raw"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve() / "raw"
                _, query, _ = _owned_schedule_evidence(root)
                if mutation == "sidecar":
                    _forge_sidecar_semantics(root, query)
                    forged = V52EvidenceStore.open_read_only(
                        root
                    ).read_verified_success(query)
                    self.assertEqual(
                        forged.query_sidecar["semantics"]["game_pks"],
                        [999999],
                    )
                else:
                    verified = V52EvidenceStore.open_read_only(
                        root
                    ).read_verified_success(query)
                    raw_path = root / str(verified.success_receipt["fetched_raw_path"])
                    raw_path.write_bytes(raw_path.read_bytes() + b" ")
                before = _snapshot_tree(root)

                with self.assertRaisesRegex(
                    (RuntimeError, ValueError),
                    "raw|sidecar|semantic|hash|identity|binding|changed",
                ):
                    _read_and_recompute_query(
                        V52EvidenceStore.open_read_only(root),
                        query,
                    )

                self.assertEqual(_snapshot_tree(root), before)

    def test_raw_mutation_during_parser_recompute_fails_without_cleanup(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            _, query, _ = _owned_schedule_evidence(root)
            verified = V52EvidenceStore.open_read_only(root).read_verified_success(
                query
            )
            raw_path = root / str(verified.success_receipt["fetched_raw_path"])
            before = _snapshot_tree(root)
            attacker_snapshots: list[
                dict[Path, tuple[int, int, int, int, bytes | None]]
            ] = []

            def recompute_then_mutate(
                supplied_query: LogicalQuery,
                raw: bytes,
            ) -> object:
                semantics = recompute_query_sidecar(supplied_query, raw)
                raw_path.write_bytes(raw_path.read_bytes() + b" ")
                attacker_snapshots.append(_snapshot_tree(root))
                return semantics

            with (
                patch.object(
                    v52_verify_sources,
                    "recompute_query_sidecar",
                    side_effect=recompute_then_mutate,
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError),
                    "raw|hash|identity|binding|changed|rebind",
                ),
            ):
                v52_verify_sources._read_and_recompute_query(
                    V52EvidenceStore.open_read_only(root),
                    query,
                )

            self.assertEqual(len(attacker_snapshots), 1)
            self.assertNotEqual(attacker_snapshots[0], before)
            self.assertEqual(_snapshot_tree(root), attacker_snapshots[0])
