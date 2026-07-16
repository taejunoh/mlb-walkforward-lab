from __future__ import annotations

import ast
import hashlib
import importlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    V52EvidenceStore,
    logical_query_id,
)
from mlb_backtest.v52_source_contracts import recompute_query_sidecar
from tests.test_v52_source_schedule import _query, _schedule_raw


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "mlb_backtest/v52_verify_sources.py"
PARSER_IMPORT_GUARD_CASES = ("schedule",)
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
""".replace("__SCHEDULE_RAW__", repr(_schedule_raw()))


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


class V52VerifySourcesTest(unittest.TestCase):
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
                imported == forbidden
                or imported.startswith(f"{forbidden}.")
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
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
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
                    raw_path = root / str(
                        verified.success_receipt["fetched_raw_path"]
                    )
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
            verified = V52EvidenceStore.open_read_only(
                root
            ).read_verified_success(query)
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
