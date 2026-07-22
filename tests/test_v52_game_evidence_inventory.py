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

    def test_superseding_pair_stops_before_query_batch_replay(self) -> None:
        from mlb_backtest.v52_game_evidence_inventory import (
            build_game_evidence_inventory,
        )

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
        with self.assertRaisesRegex(TypeError, "Task 6"):
            build_game_evidence_inventory(
                ROOT,
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )
        with self.assertRaises(ValueError):
            build_game_evidence_inventory(
                ROOT,
                run_spec_path=source_run,
                acquisition_contract_path=ROOT
                / "protocols/v5_2_qd/historical_source_acquisition.json",
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


if __name__ == "__main__":
    unittest.main()
