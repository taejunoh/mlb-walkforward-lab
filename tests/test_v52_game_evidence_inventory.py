from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_evidence import (
    AttemptCapture,
    V52EvidenceStore,
    logical_query_id,
)
from mlb_backtest.v52_source_contracts import recompute_query_sidecar
from tests.test_v52_timezone_review import _fixed_verified_gate_fixture


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_game_evidence_inventory.schema.json"
INVENTORY = Path(
    "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"
)

# Task 8 source-only duplicate disposition authority.  These are the immutable
# identities produced by the approved 565534 derived query; downstream game
# inventory must carry them without retaining the old quarantined query.
_DISPOSITION_QUERY_ID = (
    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
)
_DISPOSITION_SIDECAR_SHA256 = (
    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1"
)
_DISPOSITION_RECEIPT_SHA256 = "8" * 64
_OLD_QUARANTINED_QUERY_ID = (
    "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8"
)


def _disposition_binding() -> dict[str, object]:
    return {
        "query_id": _DISPOSITION_QUERY_ID,
        "query_sidecar": {
            "path": (
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "query_sidecars/"
                f"{_DISPOSITION_SIDECAR_SHA256}.json"
            ),
            "sha256": _DISPOSITION_SIDECAR_SHA256,
        },
        "success_receipt": {
            "path": (
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                "mlb_schedule_game_feed/"
                f"{_DISPOSITION_QUERY_ID}/success.json"
            ),
            "sha256": _DISPOSITION_RECEIPT_SHA256,
        },
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _capture(query, raw: bytes) -> AttemptCapture:
    return AttemptCapture(
        started_at_utc="2026-07-15T12:00:00.000001Z",
        ended_at_utc="2026-07-15T12:00:01.000001Z",
        final_url=query.endpoint,
        http_status=200,
        response_headers=(
            ("content-type", query.expected_media_type),
            ("content-length", str(len(raw))),
        ),
        body=raw,
        error_classification=None,
    )


def _boxscore_raw(anchor: Mapping[str, object]) -> bytes:
    def side(team_id: int, starter: int, score: int, offset: int):
        players = {
            f"ID{offset + slot}": {
                "battingOrder": str(slot * 100),
                "person": {"id": offset + slot},
            }
            for slot in range(1, 10)
        }
        return {
            "pitchers": [starter],
            "players": players,
            "team": {"id": team_id},
            "teamStats": {"batting": {"runs": score}},
        }

    game_pk = int(anchor["game_pk"])
    away_score = int(anchor["away_score"])
    home_score = int(anchor["home_score"])
    return _canonical(
        {
            "teams": {
                "away": side(
                    int(anchor["away_team_id"]),
                    game_pk * 10 + 1,
                    away_score,
                    game_pk * 100,
                ),
                "home": side(
                    int(anchor["home_team_id"]),
                    game_pk * 10 + 2,
                    home_score,
                    game_pk * 100 + 20,
                ),
            }
        }
    )


def _team_raw() -> bytes:
    return _canonical(
        {
            "teams": [
                {
                    "id": 10,
                    "locationName": "Away City",
                    "name": "Away",
                    "venue": {"id": 31},
                },
                {
                    "id": 20,
                    "locationName": "Home City",
                    "name": "Home",
                    "venue": {"id": 31},
                },
            ]
        }
    )


def _pitching_raw() -> bytes:
    return _canonical(
        {
            "stats": [
                {
                    "group": {"displayName": "pitching"},
                    "splits": [
                        {"stat": {"battersFaced": 40, "earnedRuns": 3, "outs": 27}}
                    ],
                    "totalSplits": 1,
                    "type": {"displayName": "byDateRange"},
                }
            ]
        }
    )


def _complete_reference_successes(root: Path, reference_batch, evidence_root: Path) -> None:
    inventory = json.loads(
        (root / "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json").read_bytes()
    )
    anchors = {int(row["game_pk"]): row for row in inventory["anchors"]}
    store = V52EvidenceStore(evidence_root)
    for query in reference_batch.queries:
        if store.read_verified_query_progress(query).status == "SUCCESS":
            continue
        if query.endpoint == "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING":
            semantics = {
                **recompute_query_sidecar(query, b""),
                "reason": query.requested_range["reason"],
            }
            with store.acquire_query(query):
                store.publish_derived_disposition(query, semantics)
            continue
        if "/game/" in query.endpoint and query.endpoint.endswith("/boxscore"):
            raw = _boxscore_raw(anchors[int(query.requested_range["game_pk"])])
        elif query.endpoint == "https://statsapi.mlb.com/api/v1/stats":
            raw = _pitching_raw()
        elif query.endpoint == "https://statsapi.mlb.com/api/v1/teams":
            raw = _team_raw()
        else:
            raise AssertionError(f"unexpected unfinished reference query: {query.endpoint}")
        semantics = recompute_query_sidecar(query, raw)
        with store.acquire_query(query) as lease:
            store.publish_fetched(
                query,
                store.next_attempt_number(lease),
                _capture(query, raw),
                semantics,
            )


def _fixture(root: Path):
    batches, evidence_root = _fixed_verified_gate_fixture(root)
    static_batch, reference_batch = batches
    _complete_reference_successes(root, reference_batch, evidence_root)
    return static_batch, reference_batch, evidence_root


def _query_directory(evidence_root: Path, query) -> Path:
    return (
        evidence_root
        / "queries"
        / query.source
        / logical_query_id(query)
    )


class V52GameEvidenceInventoryTest(unittest.TestCase):
    def test_timezone_adapter_derives_superseding_acquisition_paths(self) -> None:
        import mlb_backtest.v52_game_evidence_inventory as inventory_module
        from mlb_backtest.v52_query_plan import _load_static_contracts

        context = _load_static_contracts(
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json",
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json",
        )
        paths = inventory_module._timezone_authority_relatives(context)
        acquisition = Path(
            "audit/v5_2_qd/historical_superseding_565534/acquisition"
        )
        self.assertEqual(
            paths,
            (
                acquisition / "venue_timezone_candidate.json",
                acquisition / "timezone_reviews/coverage.json",
                acquisition / "timezone_reviews/semantic.json",
                Path("config/v5_2_qd/venue_timezones.json"),
                acquisition / "venue_timezone_seal.json",
            ),
        )

    def test_default_authority_rejects_external_symlink_without_fallback(self) -> None:
        import mlb_backtest.v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "repo"
            protocol = root / "protocols/v5_2_qd"
            protocol.mkdir(parents=True)
            external = base / "external-run.json"
            external.write_bytes(
                (ROOT / "protocols/v5_2_qd/historical_walkforward_500.json").read_bytes()
            )
            (protocol / "historical_walkforward_500.json").symlink_to(external)
            (protocol / "historical_source_acquisition.json").write_bytes(
                (
                    ROOT
                    / "protocols/v5_2_qd/historical_source_acquisition.json"
                ).read_bytes()
            )
            with self.assertRaises(ValueError):
                inventory_module._authority_context(root, None, None)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_default_authority_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "repo"
            protocol = root / "protocols/v5_2_qd"
            protocol.mkdir(parents=True)
            os.mkfifo(protocol / "historical_walkforward_500.json")
            (protocol / "historical_source_acquisition.json").write_bytes(
                (
                    ROOT
                    / "protocols/v5_2_qd/historical_source_acquisition.json"
                ).read_bytes()
            )
            script = (
                "from pathlib import Path; "
                "from mlb_backtest.v52_game_evidence_inventory import _authority_context; "
                f"_authority_context(Path({str(root)!r}), None, None)"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_superseding_pair_authenticates_exact_paths_before_replay(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module
        from mlb_backtest.protocols import SourceRunSpec

        source_run = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        context = inventory_module._authority_context(
            ROOT, source_run, source_contract
        )
        self.assertIsInstance(context.authority, SourceRunSpec)
        self.assertEqual(
            context.run_id,
            "historical_walkforward_500_superseding_565534",
        )
        self.assertEqual(
            context.acquisition_audit_root,
            ROOT / "audit/v5_2_qd/historical_superseding_565534/acquisition",
        )
        with self.assertRaisesRegex(ValueError, "pair|contract|authority"):
            inventory_module._authority_context(
                ROOT,
                source_run,
                ROOT / "protocols/v5_2_qd/historical_source_acquisition.json",
            )

    @staticmethod
    def _tree_byte_inode_snapshot(
        root: Path,
    ) -> dict[Path, tuple[int, int, bytes | None]]:
        return {
            path.relative_to(root): (
                path.lstat().st_dev,
                path.lstat().st_ino,
                path.read_bytes() if path.is_file() else None,
            )
            for path in (root, *sorted(root.rglob("*")))
        }

    def test_publish_load_are_closed_schema_valid_frozen_and_idempotent(self) -> None:
        from mlb_backtest.v52_game_evidence_inventory import (
            VerifiedGameEvidenceInventory,
            load_verified_game_evidence_inventory,
            publish_game_evidence_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            static_batch, reference_batch, _ = _fixture(root)
            published = publish_game_evidence_inventory(root)
            self.assertIsInstance(published, VerifiedGameEvidenceInventory)
            self.assertTrue(dataclasses.is_dataclass(published))
            self.assertTrue(published.__dataclass_params__.frozen)
            self.assertEqual(published.path, root / INVENTORY)
            payload = _thaw(published.payload)
            self.assertEqual(
                set(payload),
                {
                    "acquisition_contract",
                    "artifact_kind",
                    "counts",
                    "games",
                    "query_batches",
                    "run_id",
                    "run_spec",
                    "schedule_inventory",
                    "schema_version",
                    "source_query_sidecar_sha256s",
                    "timezone_gate",
                },
            )
            self.assertEqual(payload["artifact_kind"], "GAME_EVIDENCE_INVENTORY")
            self.assertEqual(
                [row["batch_id"] for row in payload["query_batches"]],
                [static_batch.batch_id, reference_batch.batch_id],
            )
            self.assertEqual(payload["counts"], {
                "game_count": 8,
                "played_game_count": 7,
                "unplayed_game_count": 1,
                "unique_actual_starter_count": 14,
                "verified_feature_consumer_game_count": 7,
            })
            self.assertEqual(len(payload["games"]), 8)
            played = [row for row in payload["games"] if row["played"]]
            unplayed = [row for row in payload["games"] if not row["played"]]
            self.assertTrue(
                all(
                    len(row["actual_starter_ids"]) == 2
                    and len(row["away_starting_hitter_ids"]) == 9
                    and len(row["home_starting_hitter_ids"]) == 9
                    and row["participant_provenance"]
                    == "RECONSTRUCTED_FINAL_PARTICIPANTS"
                    for row in played
                )
            )
            self.assertEqual(unplayed[0]["actual_starter_ids"], [])
            self.assertEqual(
                unplayed[0]["participant_provenance"],
                "HISTORICAL_NO_PARTICIPANTS",
            )
            jsonschema.validate(payload, json.loads(SCHEMA.read_bytes()))
            with self.assertRaises(TypeError):
                published.payload["games"][0]["game_pk"] = 1
            before = published.path.stat()
            self.assertEqual(publish_game_evidence_inventory(root), published)
            after = published.path.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(load_verified_game_evidence_inventory(root), published)

    def test_read_only_rebound_fails_closed_on_recovery_race_without_cleanup(
        self,
    ) -> None:
        from mlb_backtest import v52_game_evidence_inventory
        from mlb_backtest.v52_game_evidence_inventory import (
            load_verified_game_evidence_inventory,
            publish_game_evidence_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            static_batch, _, evidence_root = _fixture(root)
            publish_game_evidence_inventory(root)
            target = _query_directory(evidence_root, static_batch.queries[0])
            attacker_snapshot: dict[
                Path, tuple[int, int, bytes | None]
            ] | None = None

            def inject_recovery() -> None:
                nonlocal attacker_snapshot
                recovery = target / "failure-transaction"
                recovery.mkdir()
                (recovery / "canary").write_bytes(b"must-not-change")
                attacker_snapshot = self._tree_byte_inode_snapshot(root)

            with (
                patch.object(
                    v52_game_evidence_inventory,
                    "_before_game_inventory_final_rebind",
                    side_effect=inject_recovery,
                ),
                patch.object(
                    V52EvidenceStore,
                    "_finish_failure_cleanup_before_read",
                    side_effect=AssertionError("read-only cleanup reached"),
                ) as before_cleanup,
                patch.object(
                    V52EvidenceStore,
                    "_finish_failure_cleanup_tombstones",
                    side_effect=AssertionError("read-only cleanup reached"),
                ) as tombstone_cleanup,
                self.assertRaisesRegex(
                    RuntimeError,
                    "read-only evidence requires failure recovery",
                ),
            ):
                load_verified_game_evidence_inventory(
                    root,
                    read_only_progress=True,
                )

            self.assertIsNotNone(attacker_snapshot)
            self.assertEqual(
                self._tree_byte_inode_snapshot(root), attacker_snapshot
            )
            before_cleanup.assert_not_called()
            tombstone_cleanup.assert_not_called()

    def test_load_rejects_raw_sidecar_and_saved_artifact_tamper(self) -> None:
        from mlb_backtest.v52_game_evidence_inventory import (
            load_verified_game_evidence_inventory,
            publish_game_evidence_inventory,
        )

        for mutation in ("raw", "sidecar", "saved"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, reference_batch, evidence_root = _fixture(root)
                published = publish_game_evidence_inventory(root)
                boxscore = next(query for query in reference_batch.queries if "/game/" in query.endpoint)
                query_dir = evidence_root / "queries" / boxscore.source / hashlib.sha256(
                    json.dumps(dataclasses.asdict(boxscore), allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest()
                if mutation == "raw":
                    target = next((query_dir / "objects").iterdir())
                    target.write_bytes(target.read_bytes() + b" ")
                elif mutation == "sidecar":
                    target = next((query_dir / "sidecars").iterdir())
                    target.write_bytes(target.read_bytes().replace(b'"schema_version":1', b'"schema_version":2', 1))
                else:
                    payload = json.loads(published.path.read_bytes())
                    payload["counts"]["played_game_count"] -= 1
                    published.path.write_bytes(_canonical(payload))
                with self.assertRaises((ValueError, RuntimeError)):
                    load_verified_game_evidence_inventory(root)

    def test_same_byte_inode_swap_during_final_rebind_is_rejected(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            published = inventory_module.publish_game_evidence_inventory(root)
            contents = published.path.read_bytes()

            def swap() -> None:
                published.path.unlink()
                published.path.write_bytes(contents)

            with (
                patch.object(
                    inventory_module,
                    "_before_game_inventory_final_rebind",
                    side_effect=swap,
                ),
                self.assertRaisesRegex(ValueError, "identity|changed"),
            ):
                inventory_module.load_verified_game_evidence_inventory(root)

    def test_orphan_quarantine_directory_invalidates_terminal_success(self) -> None:
        from mlb_backtest.v52_game_evidence_inventory import (
            load_verified_game_evidence_inventory,
            publish_game_evidence_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, reference_batch, evidence_root = _fixture(root)
            publish_game_evidence_inventory(root)
            boxscore = next(
                query
                for query in reference_batch.queries
                if "/game/" in query.endpoint
            )
            (
                evidence_root
                / "quarantine"
                / boxscore.source
                / logical_query_id(boxscore)
            ).mkdir(parents=True)
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "quarantine|orphan|terminal"
            ):
                load_verified_game_evidence_inventory(root)

    def test_boxscore_artifact_inode_swaps_during_final_rebind_are_rejected(
        self,
    ) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        for artifact in ("raw", "sidecar", "success"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, reference_batch, evidence_root = _fixture(root)
                inventory_module.publish_game_evidence_inventory(root)
                boxscore = next(
                    query
                    for query in reference_batch.queries
                    if "/game/" in query.endpoint
                )
                query_dir = _query_directory(evidence_root, boxscore)
                if artifact == "raw":
                    target = next((query_dir / "objects").iterdir())
                elif artifact == "sidecar":
                    target = next((query_dir / "sidecars").iterdir())
                else:
                    target = query_dir / "success.json"
                contents = target.read_bytes()

                def swap() -> None:
                    target.unlink()
                    target.write_bytes(contents)

                with (
                    patch.object(
                        inventory_module,
                        "_before_game_inventory_final_rebind",
                        side_effect=swap,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "identity|changed|raw|sidecar|success",
                    ),
                ):
                    inventory_module.load_verified_game_evidence_inventory(root)

    def test_concurrent_exact_publishers_converge_on_one_inode(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            barrier = threading.Barrier(2)
            local = threading.local()
            original = inventory_module._build_current_authority

            def synchronized_build(*args, **kwargs):
                if not getattr(local, "waited", False):
                    local.waited = True
                    barrier.wait(timeout=10)
                return original(*args, **kwargs)

            def publish(_worker: int):
                value = inventory_module.publish_game_evidence_inventory(root)
                stat_result = value.path.stat()
                return value, (stat_result.st_dev, stat_result.st_ino)

            with (
                patch.object(
                    inventory_module,
                    "_build_current_authority",
                    side_effect=synchronized_build,
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                results = list(pool.map(publish, range(2)))
            self.assertEqual(results[0][0], results[1][0])
            self.assertEqual(results[0][1], results[1][1])

    def test_concurrent_publisher_reconciles_exposed_partial_final(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            expected = inventory_module._canonical_file_bytes(
                inventory_module._build_current_authority(root).payload
            )
            first_temp_write = threading.Event()
            allow_first_write = threading.Event()
            partial_final_seen = threading.Event()
            local = threading.local()
            original_write = inventory_module.os.write
            original_read = inventory_module._read_inventory_at

            def paused_write(descriptor: int, contents: object) -> int:
                if getattr(local, "worker", None) == 0 and not getattr(
                    local, "paused", False
                ):
                    local.paused = True
                    first_temp_write.set()
                    if not allow_first_write.wait(timeout=10):
                        raise RuntimeError("first publication was not released")
                return original_write(descriptor, contents)

            def observed_final_read(*args, **kwargs):
                result = original_read(*args, **kwargs)
                if result is not None and result[0] != expected:
                    partial_final_seen.set()
                return result

            def publish(worker: int):
                local.worker = worker
                value = inventory_module.publish_game_evidence_inventory(root)
                status = value.path.stat()
                return value, (status.st_dev, status.st_ino)

            with (
                patch.object(
                    inventory_module.os,
                    "write",
                    side_effect=paused_write,
                ),
                patch.object(
                    inventory_module,
                    "_read_inventory_at",
                    side_effect=observed_final_read,
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(publish, 0)
                try:
                    self.assertTrue(first_temp_write.wait(timeout=10))
                    self.assertFalse((root / INVENTORY).exists())
                    second = pool.submit(publish, 1)
                    second_result = second.result(timeout=10)
                    self.assertEqual((root / INVENTORY).read_bytes(), expected)
                    allow_first_write.set()
                    first_result = first.result(timeout=10)
                finally:
                    allow_first_write.set()
            self.assertFalse(partial_final_seen.is_set())
            self.assertEqual(first_result[0], second_result[0])
            self.assertEqual(first_result[1], second_result[1])

    def test_authority_retains_only_lightweight_evidence_snapshots(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            authority = inventory_module._build_current_authority(root)
            self.assertTrue(authority.snapshots)
            records = [
                getattr(bound, "snapshot", bound) for bound in authority.snapshots
            ]
            retained_bytes = [
                index
                for index, record in enumerate(records)
                if hasattr(record, "contents")
                or any(isinstance(value, bytes) for value in vars(record).values())
            ]
            self.assertEqual(retained_bytes, [])
            for record in records:
                names = set(record) if isinstance(record, Mapping) else set(vars(record))
                self.assertTrue({"identity", "sha256"} <= names)
                self.assertTrue({"size", "byte_count"} & names)
                self.assertTrue({"path", "relative"} & names)

    def test_public_apis_reject_root_with_symlinked_ancestor(self) -> None:
        from mlb_backtest.v52_game_evidence_inventory import (
            load_verified_game_evidence_inventory,
            publish_game_evidence_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real_parent = base / "real-parent"
            root = real_parent / "root"
            root.mkdir(parents=True)
            _fixture(root)
            publish_game_evidence_inventory(root)
            alias_parent = base / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            alias_root = alias_parent / "root"
            for operation in (
                publish_game_evidence_inventory,
                load_verified_game_evidence_inventory,
            ):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(
                    ValueError, "root|symlink|ancestor"
                ):
                    operation(alias_root)

    def test_runtime_rejects_incomplete_played_participant_projection(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            original = inventory_module.recompute_game_evidence_snapshot

            def incomplete(*args, **kwargs):
                result = json.loads(_canonical(original(*args, **kwargs)))
                played = next(row for row in result["games"] if row["played"])
                played["away_starting_hitter_ids"].pop()
                return result

            with (
                patch.object(
                    inventory_module,
                    "recompute_game_evidence_snapshot",
                    side_effect=incomplete,
                ),
                self.assertRaisesRegex(ValueError, "participant|hitter|complete"),
            ):
                inventory_module.publish_game_evidence_inventory(root)

    def test_runtime_rejects_closed_projection_contract_violations(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        def mutate(result: dict[str, object], mutation: str) -> None:
            games = result["games"]
            assert isinstance(games, list)
            played = next(row for row in games if row["played"])
            if mutation == "extra-key":
                played["unexpected"] = True
            elif mutation == "string-id":
                played["actual_starter_ids"][0] = str(
                    played["actual_starter_ids"][0]
                )
            elif mutation == "missing-boxscore":
                del played["boxscore_query_sidecar_sha256"]
            elif mutation == "invalid-date":
                played["official_date"] = "2024-02-30"
            else:
                raise AssertionError(f"unknown projection mutation: {mutation}")

        for mutation in (
            "extra-key",
            "string-id",
            "missing-boxscore",
            "invalid-date",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _fixture(root)
                original = inventory_module.recompute_game_evidence_snapshot

                def invalid(*args, **kwargs):
                    result = json.loads(_canonical(original(*args, **kwargs)))
                    mutate(result, mutation)
                    return result

                with (
                    patch.object(
                        inventory_module,
                        "recompute_game_evidence_snapshot",
                        side_effect=invalid,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "participant|identity|boxscore|date|projection|schema|field|key|complete",
                    ),
                ):
                    inventory_module.publish_game_evidence_inventory(root)

    def test_source_provenance_hash_mutation_is_rejected_at_publication_final_rebind(
        self,
    ) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module
        from mlb_backtest.v52_source_contracts import canonical_sha256

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            authority = inventory_module._build_current_authority(root)
            source_payload = _thaw(authority.payload)
            source_payload["run_id"] = "historical_walkforward_500_superseding_565534"

            def rehash(game: dict[str, object]) -> None:
                game["source_game_sha256"] = canonical_sha256(
                    {
                        "anchor_sidecar_sha256": game[
                            "schedule_query_sidecar_sha256"
                        ],
                        "boxscore_query_sidecar_sha256": game[
                            "boxscore_query_sidecar_sha256"
                        ],
                        "game": {
                            key: value
                            for key, value in game.items()
                            if key != "source_game_sha256"
                        },
                    }
                )

            for game in source_payload["games"]:
                if game["played"]:
                    game["raw_game_object_sha256"] = "a" * 64
                    game["normalized_anchor_sha256"] = "b" * 64
                    rehash(game)
            source_snapshot = {
                "games": source_payload["games"],
                "grain": "season_game_evidence_snapshot",
                "schema_version": 1,
                "source_query_sidecar_sha256s": source_payload[
                    "source_query_sidecar_sha256s"
                ],
            }
            inventory_module._validate_game_projection(
                source_snapshot,
                run_id=source_payload["run_id"],
            )
            source_authority = dataclasses.replace(
                authority,
                payload=source_payload,
                context=dataclasses.replace(
                    authority.context, run_id=source_payload["run_id"]
                ),
            )

            for field in ("raw_game_object_sha256", "normalized_anchor_sha256"):
                changed_payload = json.loads(_canonical(source_payload))
                changed_game = next(
                    game for game in changed_payload["games"] if game["played"]
                )
                changed_game[field] = "c" * 64
                rehash(changed_game)
                inventory_module._validate_game_projection(
                    {
                        "games": changed_payload["games"],
                        "grain": "season_game_evidence_snapshot",
                        "schema_version": 1,
                        "source_query_sidecar_sha256s": changed_payload[
                            "source_query_sidecar_sha256s"
                        ],
                    },
                    run_id=changed_payload["run_id"],
                )
                changed = dataclasses.replace(source_authority, payload=changed_payload)
                with self.subTest(field=field), patch.object(
                    inventory_module,
                    "_build_current_authority",
                    side_effect=(source_authority, changed),
                ), self.assertRaisesRegex(ValueError, "authority|changed|rebind"):
                    inventory_module.publish_game_evidence_inventory(root)
                self.assertFalse((root / INVENTORY).exists())

    def test_oversized_serialized_inventory_fails_before_final_publish(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            authority = inventory_module._build_current_authority(root)
            original = inventory_module._canonical_file_bytes

            def oversized(value: object) -> bytes:
                if (
                    isinstance(value, Mapping)
                    and value.get("artifact_kind") == "GAME_EVIDENCE_INVENTORY"
                ):
                    return b"x" * 1025
                return original(value)

            with (
                patch.object(inventory_module, "_MAX_ARTIFACT_BYTES", 1024),
                patch.object(
                    inventory_module,
                    "_build_current_authority",
                    return_value=authority,
                ),
                patch.object(inventory_module, "_rebind_authority"),
                patch.object(
                    inventory_module,
                    "_canonical_file_bytes",
                    side_effect=oversized,
                ),
                self.assertRaisesRegex(ValueError, "size|large|limit|bytes"),
            ):
                inventory_module.publish_game_evidence_inventory(root)
            self.assertFalse((root / INVENTORY).exists())

    def test_trailing_rebind_failure_removes_new_final_and_retry_succeeds(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            authority = inventory_module._build_current_authority(root)
            changed_payload = json.loads(_canonical(authority.payload))
            changed_payload["counts"]["game_count"] += 1
            changed = dataclasses.replace(authority, payload=changed_payload)
            with (
                patch.object(
                    inventory_module,
                    "_build_current_authority",
                    side_effect=(authority, authority, changed),
                ),
                self.assertRaisesRegex(ValueError, "authority|changed|rebind"),
            ):
                inventory_module.publish_game_evidence_inventory(root)
            self.assertFalse((root / INVENTORY).exists())
            retried = inventory_module.publish_game_evidence_inventory(root)
            self.assertEqual(retried.path, root / INVENTORY)
            self.assertEqual(retried.path.read_bytes(), _canonical(_thaw(retried.payload)))

    def test_trailing_rebind_failure_never_removes_preexisting_winner(self) -> None:
        from mlb_backtest import v52_game_evidence_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            winner = inventory_module.publish_game_evidence_inventory(root)
            winner_contents = winner.path.read_bytes()
            winner_stat = winner.path.stat()
            authority = inventory_module._build_current_authority(root)
            changed_payload = json.loads(_canonical(authority.payload))
            changed_payload["counts"]["game_count"] += 1
            changed = dataclasses.replace(authority, payload=changed_payload)
            with (
                patch.object(
                    inventory_module,
                    "_build_current_authority",
                    side_effect=(authority, authority, changed),
                ),
                self.assertRaisesRegex(ValueError, "authority|changed|rebind"),
            ):
                inventory_module.publish_game_evidence_inventory(root)
            rebound = winner.path.stat()
            self.assertEqual(winner.path.read_bytes(), winner_contents)
            self.assertEqual(
                (rebound.st_dev, rebound.st_ino),
                (winner_stat.st_dev, winner_stat.st_ino),
            )

    def test_actual_source_run_verified_load_reaches_both_final_rebinds(self) -> None:
        """The authenticated SourceRunSpec must use the real verified-load path."""
        from mlb_backtest import v52_game_evidence_inventory as inventory_module
        from mlb_backtest.protocols import SourceRunSpec

        source_run = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        context = inventory_module._authority_context(
            ROOT, source_run, source_contract
        )
        self.assertIsInstance(context.authority, SourceRunSpec)
        inventory_relative = (
            context.acquisition_audit_root.relative_to(ROOT)
            / "game_evidence_inventory.json"
        )
        payload = {
            "artifact_kind": "GAME_EVIDENCE_INVENTORY",
            "run_id": context.run_id,
            "schema_version": 1,
        }
        snapshot = SimpleNamespace(
            contents=_canonical(payload),
            identity=(1, 2, 3),
            relative=inventory_relative,
        )
        authority = inventory_module._Authority(payload, (), context)

        with (
            patch.object(
                inventory_module, "_required_snapshot", return_value=snapshot
            ) as read_inventory,
            patch.object(
                inventory_module,
                "_build_current_authority",
                side_effect=(authority, authority),
            ) as build_authority,
            patch.object(inventory_module, "_rebind_authority") as rebind_authority,
            patch.object(
                inventory_module, "_rebind_safe_snapshot"
            ) as rebind_inventory,
            patch.object(
                inventory_module, "_before_game_inventory_final_rebind"
            ) as final_hook,
        ):
            loaded = inventory_module.load_verified_game_evidence_inventory(
                ROOT,
                read_only_progress=True,
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )

        self.assertEqual(loaded.path, ROOT / inventory_relative)
        self.assertEqual(_thaw(loaded.payload), payload)
        read_inventory.assert_called_once_with(
            ROOT, inventory_relative, "game evidence inventory"
        )
        self.assertEqual(build_authority.call_count, 2)
        final_hook.assert_called_once_with()
        rebind_authority.assert_called_once_with(ROOT, authority)
        rebind_inventory.assert_called_once_with(
            ROOT,
            snapshot,
            "game evidence inventory",
            max_bytes=inventory_module._MAX_ARTIFACT_BYTES,
        )

    def test_source_build_authenticates_disposition_semantics_and_adapter(self) -> None:
        """The source build traverses the authority-only derived query branch."""
        from mlb_backtest import v52_game_evidence_inventory as inventory_module
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            DISPOSITION_ENDPOINT,
            _requested_range,
        )
        from mlb_backtest.v52_evidence import LogicalQuery

        source_run = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        context = inventory_module._authority_context(
            ROOT, source_run, source_contract
        )
        disposition_query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=DISPOSITION_ENDPOINT,
            parameters={},
            requested_range=_requested_range(),
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256=context.authority_sha256,
            acquisition_contract_sha256=context.acquisition_contract_sha256,
        )
        self.assertEqual(logical_query_id(disposition_query), _DISPOSITION_QUERY_ID)
        schedule_query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint="https://statsapi.mlb.com/api/v1/schedule",
            parameters={},
            requested_range={"year": 2018},
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256=context.authority_sha256,
            acquisition_contract_sha256=context.acquisition_contract_sha256,
        )
        schedule_contents = _canonical(
            {"static_batch": {"batch_id": "1" * 64}}
        )
        schedule_sha = hashlib.sha256(schedule_contents).hexdigest()
        static_batch = SimpleNamespace(
            batch_id="1" * 64,
            stage="STATIC_SCHEDULES",
            derived_from_sha256=None,
            queries=(schedule_query, disposition_query),
        )
        reference_batch = SimpleNamespace(
            batch_id="2" * 64,
            stage="REFERENCE_INPUTS",
            derived_from_sha256=schedule_sha,
            queries=(),
        )
        source_binding = _disposition_binding()
        season_sources = [
            {"season": season}
            for season in range(2018, 2025)
        ]
        season_sources[1] = {
            "season": 2019,
            "storage_mode": "DERIVED_DISPOSITION",
            "raw_sha256": None,
            **source_binding,
        }
        verified_schedule = SimpleNamespace(
            sha256=schedule_sha,
            payload={
                "anchors": [
                    {
                        "alternate_site": False,
                        "away_score": 2,
                        "away_team_id": 10,
                        "completed_innings": 9,
                        "doubleheader_order": 1,
                        "game_pk": 565534,
                        "home_score": 4,
                        "home_team_id": 20,
                        "neutral_site": False,
                        "official_date": "2019-09-06",
                        "played": True,
                        "raw_game_object_sha256": "9" * 64,
                        "scheduled_innings": 9,
                        "scheduled_start_utc": "2019-09-07T00:15:00Z",
                        "season": 2019,
                        "season_start_date": "2019-03-20",
                        "terminal_status": "Final",
                        "venue_id": 31,
                        "winner_team_id": 20,
                    }
                ],
                "season_sources": season_sources,
            },
        )

        snapshot_contents = {
            "game evidence RunSpec": source_run.read_bytes(),
            "game evidence acquisition contract": source_contract.read_bytes(),
            "game evidence schedule inventory": schedule_contents,
            "game evidence STATIC_SCHEDULES batch": _canonical({"id": "1" * 64}),
            "game evidence REFERENCE_INPUTS batch": _canonical({"id": "2" * 64}),
            "game evidence timezone candidate": _canonical({"kind": "candidate"}),
            "game evidence timezone coverage review": _canonical({"kind": "coverage"}),
            "game evidence timezone semantic review": _canonical({"kind": "semantic"}),
            "game evidence timezone mapping": _canonical({"kind": "mapping"}),
            "game evidence timezone seal": _canonical({"kind": "seal"}),
        }
        identity = iter(range(1, 100))

        def required_snapshot(_root, relative, label, **_kwargs):
            return SimpleNamespace(
                contents=snapshot_contents[label],
                identity=(next(identity),),
                relative=relative,
            )

        timezone_gate = SimpleNamespace(
            run_spec_sha256=hashlib.sha256(
                snapshot_contents["game evidence RunSpec"]
            ).hexdigest(),
            acquisition_contract_sha256=hashlib.sha256(
                snapshot_contents["game evidence acquisition contract"]
            ).hexdigest(),
            schedule_inventory_sha256=schedule_sha,
            query_batch_ids=(static_batch.batch_id, reference_batch.batch_id),
            query_batch_sha256s=(
                hashlib.sha256(
                    snapshot_contents["game evidence STATIC_SCHEDULES batch"]
                ).hexdigest(),
                hashlib.sha256(
                    snapshot_contents["game evidence REFERENCE_INPUTS batch"]
                ).hexdigest(),
            ),
            candidate_sha256=hashlib.sha256(
                snapshot_contents["game evidence timezone candidate"]
            ).hexdigest(),
            review_sha256s=(
                hashlib.sha256(
                    snapshot_contents["game evidence timezone coverage review"]
                ).hexdigest(),
                hashlib.sha256(
                    snapshot_contents["game evidence timezone semantic review"]
                ).hexdigest(),
            ),
            mapping_sha256=hashlib.sha256(
                snapshot_contents["game evidence timezone mapping"]
            ).hexdigest(),
            seal_sha256=hashlib.sha256(
                snapshot_contents["game evidence timezone seal"]
            ).hexdigest(),
        )
        ordinary_semantics = {"season": 2018}
        disposition_semantics = {"authenticated_projection": True}

        def verified(query, semantics, raw, storage_mode, sidecar_sha):
            published = SimpleNamespace(
                storage_mode=storage_mode,
                query_sidecar_sha256=sidecar_sha,
            )
            return SimpleNamespace(
                registered_query=query,
                published_evidence=published,
                attempt_receipts=(),
                query_sidecar={
                    "source": query.source,
                    "grain": query.grain,
                    "parser_version": query.parser_version,
                    "schema_version": 1,
                    "query_id": logical_query_id(query),
                    "semantics": semantics,
                },
                raw_bytes=raw,
                success_receipt={"storage_mode": storage_mode},
            )

        ordinary_verified = verified(
            schedule_query,
            ordinary_semantics,
            b"{}",
            "FETCHED_RAW",
            "7" * 64,
        )
        disposition_verified = verified(
            disposition_query,
            disposition_semantics,
            None,
            "DERIVED_DISPOSITION",
            _DISPOSITION_SIDECAR_SHA256,
        )
        def verified_for(query):
            return (
                disposition_verified
                if query == disposition_query
                else ordinary_verified
            )
        authority_calls: list[tuple[str, object | None]] = []

        class FakeStore:
            def read_verified_query_progress(self, query, *, authority=None):
                authority_calls.append(("progress", authority))
                value = verified_for(query)
                return SimpleNamespace(
                    status="SUCCESS",
                    registered_query=query,
                    published_evidence=value.published_evidence,
                    pending_attempt_numbers=(),
                    quarantines=(),
                    attempt_receipts=(),
                )

            def read_verified_success(self, query, *, authority=None):
                authority_calls.append(("success", authority))
                return verified_for(query)

        disposition_authority = SimpleNamespace(
            query=disposition_query,
            reauthenticate=lambda: disposition_authority,
        )
        captured_adapters: list[Mapping[str, object]] = []

        captured_provenance: list[Mapping[int, Mapping[str, object]]] = []

        def game_snapshot(schedule_sidecars, _boxscores, *, schedule_provenance):
            captured_adapters.extend(schedule_sidecars)
            captured_provenance.append(schedule_provenance)
            adapter = schedule_sidecars[-1]
            adapter_sha = hashlib.sha256(_canonical(adapter)).hexdigest()
            return {
                "games": [
                    {
                        "actual_starter_ids": [],
                        "played": False,
                        "schedule_query_sidecar_sha256": adapter_sha,
                        "season": 2019,
                        "source_game_sha256": "6" * 64,
                    }
                ],
                "source_query_sidecar_sha256s": [adapter_sha],
            }

        def recompute(query, raw, *, authority=None):
            if query == disposition_query:
                self.assertIs(authority, disposition_authority)
                self.assertEqual(raw, b"")
                return disposition_semantics
            self.assertIsNone(authority)
            return ordinary_semantics

        with (
            patch.object(
                inventory_module,
                "_required_snapshot",
                side_effect=required_snapshot,
            ),
            patch.object(
                inventory_module,
                "load_verified_schedule_anchor_inventory",
                return_value=verified_schedule,
            ),
            patch.object(
                inventory_module,
                "_load_authority_batch",
                side_effect=(static_batch, reference_batch),
            ),
            patch.object(
                inventory_module,
                "build_schedule_reference_batch",
                return_value=reference_batch,
            ),
            patch.object(
                inventory_module,
                "load_verified_timezone_gate",
                return_value=timezone_gate,
            ),
            patch.object(
                inventory_module.V52EvidenceStore,
                "open_read_only",
                return_value=FakeStore(),
            ),
            patch(
                "mlb_backtest.v52_duplicate_gamepk_disposition."
                "build_duplicate_disposition_authority",
                return_value=disposition_authority,
            ),
            patch.object(
                inventory_module,
                "_verified_evidence_snapshots",
                return_value=(),
            ),
            patch.object(
                inventory_module,
                "recompute_query_sidecar",
                side_effect=recompute,
            ),
            patch.object(
                inventory_module,
                "recompute_schedule_anchor_sidecar",
                return_value=ordinary_verified.query_sidecar,
            ),
            patch.object(
                inventory_module,
                "recompute_game_evidence_snapshot",
                side_effect=game_snapshot,
            ),
            patch.object(inventory_module, "_validate_game_projection"),
        ):
            built = inventory_module._build_current_authority(
                ROOT,
                read_only_progress=True,
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )

        self.assertEqual(
            built.payload["duplicate_gamepk_disposition"], source_binding
        )
        self.assertEqual(
            built.payload["dispositioned_query_ids"], [_DISPOSITION_QUERY_ID]
        )
        self.assertEqual(
            built.payload["source_query_sidecar_sha256s"],
            [_DISPOSITION_SIDECAR_SHA256],
        )
        adapter = captured_adapters[-1]
        self.assertEqual(
            set(adapter),
            {
                "byte_count",
                "grain",
                "parser_version",
                "query_id",
                "raw_sha256",
                "schema_version",
                "semantics",
                "source",
            },
        )
        self.assertEqual(adapter["raw_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertNotIn(
            "raw_game_object_sha256", adapter["semantics"]["anchors"][0]
        )
        self.assertEqual(
            set(captured_provenance[-1]),
            {
                int(anchor["game_pk"])
                for anchor in verified_schedule.payload["anchors"]
            },
        )
        disposition_calls = [
            authority
            for _operation, authority in authority_calls
            if authority is not None
        ]
        self.assertEqual(disposition_calls, [disposition_authority] * 3)

    def test_superseding_inventory_schema_carries_only_derived_disposition_authority(
        self,
    ) -> None:
        """The new branch carries the derived receipt/sidecar and sorted ID.

        This is deliberately a schema-level RED test: the old closed schema
        must reject the source-only run until Task 8 adds its exact oneOf
        branch.  The old quarantined query ID is not part of the downstream
        binding.
        """
        payload = {
            "acquisition_contract": {
                "path": "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
                "sha256": "b" * 64,
            },
            "artifact_kind": "GAME_EVIDENCE_INVENTORY",
            "counts": {
                "game_count": 1,
                "played_game_count": 0,
                "unique_actual_starter_count": 0,
                "unplayed_game_count": 1,
                "verified_feature_consumer_game_count": 0,
            },
            "games": [{"game_pk": 565534, "season": 2019}],
            "query_batches": [
                {
                    "batch_id": "1" * 64,
                    "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/query_batches/"
                    + "1" * 64 + ".json",
                    "sha256": "1" * 64,
                    "stage": "STATIC_SCHEDULES",
                },
                {
                    "batch_id": "2" * 64,
                    "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/query_batches/"
                    + "2" * 64 + ".json",
                    "sha256": "2" * 64,
                    "stage": "REFERENCE_INPUTS",
                },
            ],
            "run_id": "historical_walkforward_500_superseding_565534",
            "run_spec": {
                "path": "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
                "sha256": "a" * 64,
            },
            "schedule_inventory": {
                "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/schedule_anchor_inventory.json",
                "sha256": "c" * 64,
            },
            "schema_version": 1,
            "source_query_sidecar_sha256s": [_DISPOSITION_SIDECAR_SHA256],
            "timezone_gate": {
                "candidate": {
                    "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/venue_timezone_candidate.json",
                    "sha256": "f" * 64,
                },
                "mapping": {
                    "path": "config/v5_2_qd/venue_timezones.json",
                    "sha256": "0" * 64,
                },
                "reviews": [
                    {
                        "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/timezone_reviews/coverage.json",
                        "sha256": "1" * 64,
                    },
                    {
                        "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/timezone_reviews/semantic.json",
                        "sha256": "2" * 64,
                    },
                ],
                "seal": {
                    "path": "audit/v5_2_qd/historical_superseding_565534/acquisition/venue_timezone_seal.json",
                    "sha256": "3" * 64,
                },
            },
            "duplicate_gamepk_disposition": _disposition_binding(),
            "dispositioned_query_ids": [_DISPOSITION_QUERY_ID],
        }
        jsonschema.validate(payload, json.loads(SCHEMA.read_bytes()))

    def test_schema_exactly_separates_old_and_source_authority_branches(self) -> None:
        """Neither authority may validate by falling through the other branch."""
        from mlb_backtest.v52_game_evidence_inventory import (
            publish_game_evidence_inventory,
        )

        schema = json.loads(SCHEMA.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture(root)
            old = _thaw(publish_game_evidence_inventory(root).payload)
        self.assertIsInstance(old, dict)
        jsonschema.validate(old, schema)

        source = json.loads(_canonical(old))
        source["run_id"] = "historical_walkforward_500_superseding_565534"
        source["run_spec"]["path"] = (
            "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source["acquisition_contract"]["path"] = (
            "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        source["schedule_inventory"]["path"] = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "schedule_anchor_inventory.json"
        )
        for batch in source["query_batches"]:
            batch["path"] = (
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                f"query_batches/{batch['batch_id']}.json"
            )
        source["timezone_gate"]["candidate"]["path"] = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "venue_timezone_candidate.json"
        )
        source["timezone_gate"]["reviews"][0]["path"] = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "timezone_reviews/coverage.json"
        )
        source["timezone_gate"]["reviews"][1]["path"] = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "timezone_reviews/semantic.json"
        )
        source["timezone_gate"]["seal"]["path"] = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "venue_timezone_seal.json"
        )
        source["duplicate_gamepk_disposition"] = _disposition_binding()
        source["dispositioned_query_ids"] = [_DISPOSITION_QUERY_ID]
        for game in source["games"]:
            if game["played"]:
                game["raw_game_object_sha256"] = "a" * 64
                game["normalized_anchor_sha256"] = "b" * 64
        jsonschema.validate(source, schema)

        incomplete_source = json.loads(_canonical(source))
        incomplete_played = next(
            game for game in incomplete_source["games"] if game["played"]
        )
        del incomplete_played["raw_game_object_sha256"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(incomplete_source, schema)

        partial_only_source = json.loads(_canonical(source))
        partial_only_source["games"] = [{"game_pk": 565534, "season": 2019}]
        jsonschema.validate(partial_only_source, schema)

        source_as_old = json.loads(_canonical(source))
        source_as_old.pop("duplicate_gamepk_disposition")
        source_as_old.pop("dispositioned_query_ids")
        old_as_source = json.loads(_canonical(old))
        old_as_source["duplicate_gamepk_disposition"] = _disposition_binding()
        old_as_source["dispositioned_query_ids"] = [_DISPOSITION_QUERY_ID]

        attacks = {
            "source-as-old": source_as_old,
            "old-as-source": old_as_source,
            "generic-sidecar-path": json.loads(_canonical(source)),
            "generic-success-path": json.loads(_canonical(source)),
            "wrong-batch-namespace": json.loads(_canonical(source)),
            "zero-game-count": json.loads(_canonical(source)),
        }
        attacks["generic-sidecar-path"]["duplicate_gamepk_disposition"][
            "query_sidecar"
        ]["path"] = "sealed/query_sidecar.json"
        attacks["generic-success-path"]["duplicate_gamepk_disposition"][
            "success_receipt"
        ]["path"] = "sealed/success.json"
        attacks["wrong-batch-namespace"]["query_batches"][0]["path"] = (
            "audit/v5_2_qd/historical/acquisition/query_batches/"
            f"{source['query_batches'][0]['batch_id']}.json"
        )
        attacks["zero-game-count"]["counts"]["game_count"] = 0
        for label, candidate in attacks.items():
            with self.subTest(label=label), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(candidate, schema)


if __name__ == "__main__":
    unittest.main()
