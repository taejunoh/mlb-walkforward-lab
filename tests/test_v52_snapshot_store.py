from __future__ import annotations

import builtins
import errno
import hashlib
import os
import re
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request
import json
from dataclasses import FrozenInstanceError, replace

from mlb_backtest import v52_verify_sources as verifier
from tests.v52_snapshot_store_fixture import (
    fake_81,
    patched_store_replay,
    replay_inputs,
    store_inputs,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplayTests(unittest.TestCase):
    def test_replay_uses_exact_sha_and_statcast_parent_coverage_key(self):
        expected = fake_81()
        by_source = {
            source: tuple(snapshot for snapshot in expected if snapshot.source == source)
            for source in {
                snapshot.source for snapshot in expected
            }
        }
        inputs = replay_inputs()
        query_ids = {record.query_id for record in inputs.records}
        sidecar_hashes = {
            record.query_sidecar_sha256 for record in inputs.records
        }
        self.assertEqual(len(query_ids), 81)
        self.assertEqual(len(sidecar_hashes), 81)
        self.assertTrue(all(_SHA256.fullmatch(value) for value in query_ids))
        self.assertTrue(all(_SHA256.fullmatch(value) for value in sidecar_hashes))
        self.assertTrue(query_ids.isdisjoint(sidecar_hashes))
        with patch(
            "mlb_backtest.v52_verify_sources._build_game_normalized_snapshots",
            return_value=by_source["mlb_schedule_game_feed"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_statcast_normalized_snapshots",
            return_value=by_source["baseball_savant_statcast"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_pitching_normalized_snapshots",
            return_value=by_source["mlb_stats_pitching"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_reference_normalized_snapshots",
            return_value=by_source["venue_team_timezone_table"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_history_normalized_snapshots",
            return_value=by_source["prior_schedule_history"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_career_bf_normalized_snapshots",
            return_value=by_source["mlb_stats_pitcher_bf"],
        ), patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")), patch.object(
            socket, "socket", side_effect=AssertionError("network")
        ), patch.object(
            builtins, "__import__", side_effect=AssertionError("import")
        ), patch.object(
            builtins, "open", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            os, "open", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "mkdir", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "write_bytes", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "write_text", side_effect=AssertionError("filesystem I/O")
        ):
            replayed = verifier.replay_verified_snapshots(inputs)
        self.assertEqual(len(replayed), 81)
        self.assertEqual(len({snapshot.sha256 for snapshot in replayed}), 81)
        self.assertEqual(
            len({(snapshot.season, snapshot.source) for snapshot in replayed}), 42
        )
        self.assertEqual(replayed, tuple(sorted(replayed, key=verifier.snapshot_sort_key)))
        self.assertEqual(
            len(
                {
                    (
                        snapshot.season,
                        snapshot.payload["parent_coverage"]["inclusive_start"],
                        snapshot.payload["parent_coverage"]["inclusive_end"],
                    )
                    for snapshot in replayed
                    if snapshot.source == "baseball_savant_statcast"
                }
            ),
            46,
        )


class InventoryTests(unittest.TestCase):
    def _inventory(self):
        from mlb_backtest.v52_snapshot_store import build_inventory

        with patched_store_replay():
            return build_inventory(store_inputs())

    def test_builds_closed_canonical_inventory(self):
        inventory = self._inventory()
        payload = inventory.as_mapping()
        self.assertEqual(
            set(payload),
            {
                "schema_version", "artifact_kind", "run_id", "status", "critical_errors",
                "network_attempts", "source_gate_ready", "run_spec", "acquisition_contract",
                "terminal_checkpoint", "schedule_inventory", "game_evidence_inventory", "timezone_gate",
                "snapshot_sha256s", "logical_query_ids", "split_receipt_sha256s", "snapshots",
                "source_seasons", "counts",
            },
        )
        self.assertEqual(inventory.counts["snapshot_count"], 81)
        self.assertEqual(inventory.counts["source_season_count"], 42)
        self.assertEqual(inventory.counts["source_count"], 6)
        self.assertEqual(inventory.counts["by_season"], {2018: 12, 2019: 12, 2020: 8, 2021: 12, 2022: 12, 2023: 13, 2024: 12})
        self.assertEqual(inventory.counts["by_source"]["baseball_savant_statcast"], 46)
        self.assertTrue(all(value == 7 for key, value in inventory.counts["by_source"].items() if key != "baseball_savant_statcast"))
        self.assertEqual(payload["terminal_checkpoint"]["stage"], "REMAINING")
        self.assertEqual(
            {
                key: payload[key]
                for key in (
                    "run_spec",
                    "acquisition_contract",
                    "terminal_checkpoint",
                    "schedule_inventory",
                    "game_evidence_inventory",
                    "timezone_gate",
                )
            },
            {
                "run_spec": {"path": "audit/v5_2_qd/historical/run_spec.json", "sha256": "a" * 64},
                "acquisition_contract": {"path": "audit/v5_2_qd/historical/acquisition_contract.json", "sha256": "b" * 64},
                "terminal_checkpoint": {"path": "audit/v5_2_qd/historical/acquisition/checkpoint.json", "sha256": "c" * 64, "sequence": 81, "stage": "REMAINING"},
                "schedule_inventory": {"path": "audit/v5_2_qd/historical/schedule_inventory.json", "sha256": "d" * 64},
                "game_evidence_inventory": {"path": "audit/v5_2_qd/historical/game_evidence_inventory.json", "sha256": "e" * 64},
                "timezone_gate": {"path": "audit/v5_2_qd/historical/timezone_gate.json", "sha256": "f" * 64},
            },
        )
        self.assertEqual(len(payload["logical_query_ids"]), 81)
        self.assertEqual(payload["split_receipt_sha256s"], [])
        self.assertEqual(len(payload["snapshots"]), 81)
        self.assertEqual(len(payload["source_seasons"]), 42)
        self.assertEqual(payload["snapshot_sha256s"], [row["snapshot_sha256"] for row in payload["snapshots"]])
        self.assertEqual(
            payload["logical_query_ids"],
            sorted({query_id for row in payload["snapshots"] for query_id in row["logical_query_ids"]}),
        )
        self.assertEqual(
            [digest for row in payload["source_seasons"] for digest in row["snapshot_sha256s"]],
            payload["snapshot_sha256s"],
        )

    def test_snapshot_rows_preserve_verifier_receipt_order(self):
        from mlb_backtest.v52_snapshot_store import build_inventory
        from mlb_backtest.v52_source_contracts import canonical_json_bytes

        snapshots = list(fake_81())
        original = next(
            snapshot
            for snapshot in snapshots
            if snapshot.source == "baseball_savant_statcast"
        )
        receipt_order = ("f" * 64, "a" * 64)
        envelope = original.as_mapping()
        envelope["split_receipt_sha256s"] = list(receipt_order)
        canonical_bytes = canonical_json_bytes(envelope) + b"\n"
        replacement = replace(
            original,
            split_receipt_sha256s=receipt_order,
            canonical_bytes=canonical_bytes,
            sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        )
        snapshots[snapshots.index(original)] = replacement
        snapshots.sort(key=verifier.snapshot_sort_key)

        with patch(
            "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
            return_value=tuple(snapshots),
        ):
            inventory = build_inventory(store_inputs())

        row = next(
            row
            for row in inventory.snapshots
            if row.snapshot_sha256 == replacement.sha256
        )
        self.assertEqual(row.split_receipt_sha256s, receipt_order)
        self.assertEqual(inventory.split_receipt_sha256s, tuple(sorted(receipt_order)))
        statcast = next(
            row
            for row in inventory.source_seasons
            if row.season == replacement.season
            and row.source == replacement.source
        )
        self.assertEqual(statcast.split_receipt_sha256s, tuple(sorted(receipt_order)))

    def test_phase_does_not_change_inventory_and_values_are_immutable(self):
        from mlb_backtest.v52_snapshot_store import build_inventory

        with patched_store_replay():
            acquiring = build_inventory(store_inputs(current_phase="ACQUIRING"))
            ready = build_inventory(store_inputs(current_phase="SOURCES_READY"))
        self.assertEqual(acquiring.as_mapping(), ready.as_mapping())
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            acquiring.run_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            acquiring.counts["snapshot_count"] = 0  # type: ignore[index]

    def test_canonical_round_trip_and_schema(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        inventory = self._inventory()
        raw = inventory_bytes(inventory)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(parse_inventory(raw), inventory)
        self.assertEqual(inventory_bytes(parse_inventory(raw)), raw)
        self.assertEqual(json.loads(raw), inventory.as_mapping())

    def test_parser_rejects_noncanonical_or_semantically_invalid_values(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        raw = inventory_bytes(self._inventory())
        mutations = (
            ("extra", lambda value: value.__setitem__("extra", True)),
            ("missing", lambda value: value.pop("counts")),
            ("absolute-path", lambda value: value["snapshots"][0].__setitem__("relative_path", "/escape")),
            ("parent-path", lambda value: value["snapshots"][0].__setitem__("relative_path", "../escape")),
            ("backslash-path", lambda value: value["snapshots"][0].__setitem__("relative_path", "2018\\bad")),
            ("wrong-status", lambda value: value.__setitem__("status", "READY")),
            ("wrong-order", lambda value: value["snapshot_sha256s"].reverse()),
            ("duplicate-sha", lambda value: value["snapshot_sha256s"].__setitem__(1, value["snapshot_sha256s"][0])),
            ("duplicate-query", lambda value: value["snapshots"][0]["logical_query_ids"].append(value["snapshots"][0]["logical_query_ids"][0])),
            ("duplicate-sidecar", lambda value: value["snapshots"][0]["source_query_sidecar_sha256s"].append(value["snapshots"][0]["source_query_sidecar_sha256s"][0])),
            ("component-collision", lambda value: value["snapshots"][0]["logical_query_ids"].__setitem__(0, value["snapshots"][0]["snapshot_sha256"])),
            ("bad-grain", lambda value: value["snapshots"][0].__setitem__("grain", "wrong")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                candidate = json.loads(raw)
                mutate(candidate)
                candidate_raw = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                with self.assertRaises((TypeError, ValueError)):
                    parse_inventory(candidate_raw)
        with self.assertRaises((TypeError, ValueError)):
            parse_inventory(raw[:-1])
        with self.assertRaises((TypeError, ValueError)):
            parse_inventory("not bytes")  # type: ignore[arg-type]

    def test_parser_rejects_every_closed_count_member(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        raw = inventory_bytes(self._inventory())
        mutations = (
            ("snapshot_count", lambda value: value["counts"].__setitem__("snapshot_count", 80)),
            ("source_season_count", lambda value: value["counts"].__setitem__("source_season_count", 41)),
            ("source_count", lambda value: value["counts"].__setitem__("source_count", 5)),
            ("logical_query_count", lambda value: value["counts"].__setitem__("logical_query_count", 80)),
            ("split_receipt_count", lambda value: value["counts"].__setitem__("split_receipt_count", 1)),
            ("by_source", lambda value: value["counts"]["by_source"].__setitem__("baseball_savant_statcast", 45)),
            ("by_season", lambda value: value["counts"]["by_season"].__setitem__("2018", 11)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                candidate = json.loads(raw)
                mutate(candidate)
                with self.assertRaises((TypeError, ValueError)):
                    parse_inventory(_canonical_raw(candidate))

    def test_parser_rejects_global_component_union_mutations(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        logical_raw = inventory_bytes(self._inventory())
        logical_mutations = (
            ("missing", lambda value: value["logical_query_ids"].pop()),
            ("extra", lambda value: value["logical_query_ids"].append("f" * 64)),
            ("duplicate", lambda value: value["logical_query_ids"].append(value["logical_query_ids"][0])),
            ("wrong_order", lambda value: value["logical_query_ids"].reverse()),
        )
        for label, mutate in logical_mutations:
            with self.subTest(component="logical_query_ids", label=label):
                candidate = json.loads(logical_raw)
                mutate(candidate)
                with self.assertRaises((TypeError, ValueError)):
                    parse_inventory(_canonical_raw(candidate))

        split_raw = _raw_with_two_split_receipts(logical_raw)
        parse_inventory(split_raw)
        split_mutations = (
            ("missing", lambda value: value["split_receipt_sha256s"].pop()),
            ("extra", lambda value: value["split_receipt_sha256s"].append("f" * 64)),
            ("duplicate", lambda value: value["split_receipt_sha256s"].append(value["split_receipt_sha256s"][0])),
            ("wrong_order", lambda value: value["split_receipt_sha256s"].reverse()),
        )
        for label, mutate in split_mutations:
            with self.subTest(component="split_receipt_sha256s", label=label):
                candidate = json.loads(split_raw)
                mutate(candidate)
                with self.assertRaises((TypeError, ValueError)):
                    parse_inventory(_canonical_raw(candidate))

    def test_parser_rejects_coherently_empty_snapshot_query_and_sidecar_arrays(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        candidate = json.loads(inventory_bytes(self._inventory()))
        snapshot = candidate["snapshots"][0]
        query_id = snapshot["logical_query_ids"][0]
        snapshot["logical_query_ids"] = []
        snapshot["source_query_sidecar_sha256s"] = []
        source_season = next(
            row
            for row in candidate["source_seasons"]
            if row["season"] == snapshot["season"] and row["source"] == snapshot["source"]
        )
        source_season["logical_query_ids"].remove(query_id)
        candidate["logical_query_ids"].remove(query_id)
        candidate["counts"]["logical_query_count"] -= 1
        with self.assertRaises((TypeError, ValueError)):
            parse_inventory(_canonical_raw(candidate))

    def test_parser_rejects_control_characters_in_authority_and_snapshot_paths(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        raw = inventory_bytes(self._inventory())
        for label, control in (
            ("nul", "\x00"),
            ("newline", "\n"),
            ("tab", "\t"),
            ("unit_separator", "\x1f"),
            ("delete", "\x7f"),
        ):
            for target in ("authority", "snapshot"):
                with self.subTest(label=label, target=target):
                    candidate = json.loads(raw)
                    if target == "authority":
                        candidate["run_spec"]["path"] = f"audit/{control}run_spec.json"
                    else:
                        candidate["snapshots"][0]["relative_path"] += control
                    with self.assertRaises((TypeError, ValueError)):
                        parse_inventory(_canonical_raw(candidate))

    def test_statcast_source_season_hashes_follow_parent_coverage_order(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        inventory = self._inventory()
        expected = tuple(
            snapshot.sha256
            for snapshot in sorted(fake_81(), key=verifier.snapshot_sort_key)
            if snapshot.source == "baseball_savant_statcast" and snapshot.season == 2018
        )
        statcast = next(
            row
            for row in inventory.source_seasons
            if row.season == 2018 and row.source == "baseball_savant_statcast"
        )
        self.assertEqual(statcast.snapshot_sha256s, expected)

        candidate = json.loads(inventory_bytes(inventory))
        row = next(
            item
            for item in candidate["source_seasons"]
            if item["season"] == 2018 and item["source"] == "baseball_savant_statcast"
        )
        row["snapshot_sha256s"][0], row["snapshot_sha256s"][1] = (
            row["snapshot_sha256s"][1],
            row["snapshot_sha256s"][0],
        )
        with self.assertRaises((TypeError, ValueError)):
            parse_inventory(_canonical_raw(candidate))

    def test_build_rejects_disallowed_phase(self):
        from mlb_backtest.v52_snapshot_store import build_inventory

        with self.assertRaises(ValueError), patched_store_replay():
            build_inventory(store_inputs(current_phase="PUBLISHED"))


class PublishTests(unittest.TestCase):
    """The store's final inventory is the only externally visible commit point."""

    def _inventory(self):
        from mlb_backtest.v52_snapshot_store import build_inventory

        with patched_store_replay():
            return build_inventory(store_inputs())

    def _publish(self, root, inventory=None, inputs=None):
        from mlb_backtest.v52_snapshot_store import publish_snapshot_store

        with patched_store_replay():
            return publish_snapshot_store(
                root, inventory or self._inventory(), inputs or store_inputs(), lock_owner="publish-test"
            )

    def test_fd_capability_reports_created_then_existing_without_closing_caller_fd(self):
        from mlb_backtest.v52_snapshot_store import publish_snapshot_store_from_fd

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patched_store_replay():
                    first = publish_snapshot_store_from_fd(
                        fd, inventory, store_inputs(), lock_owner="fd-publish"
                    )
                    second = publish_snapshot_store_from_fd(
                        fd, inventory, store_inputs(), lock_owner="fd-publish"
                    )
                self.assertTrue(first.created)
                self.assertFalse(second.created)
                self.assertEqual(first.receipt, second.receipt)
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            finally:
                os.close(fd)

    def test_fd_publish_stays_on_opened_directory_after_path_is_rebound(self):
        from mlb_backtest.v52_snapshot_store import (
            inventory_bytes,
            publish_snapshot_store_from_fd,
        )

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            original = base / "source-root"
            original.mkdir()
            moved = base / "held-source-root"
            external = base / "external"
            external.mkdir()
            fd = os.open(original, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            before_fds = len(os.listdir("/dev/fd"))
            try:
                original.rename(moved)
                original.symlink_to(external, target_is_directory=True)
                with patched_store_replay():
                    outcome = publish_snapshot_store_from_fd(
                        fd, inventory, store_inputs(), lock_owner="fd-path-rebind"
                    )
                self.assertTrue(outcome.created)
                self.assertEqual(
                    moved.joinpath("source_inventory.json").read_bytes(),
                    inventory_bytes(inventory),
                )
                self.assertEqual(tuple(external.iterdir()), ())
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
            finally:
                os.close(fd)


    def test_fresh_publish_is_content_addressed_and_removes_owned_lock(self):
        from mlb_backtest.v52_snapshot_store import PublishReceipt, inventory_bytes

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            receipt = self._publish(root, inventory)
            self.assertIsInstance(receipt, PublishReceipt)
            self.assertEqual(receipt.snapshot_count, 81)
            raw_inventory = root.joinpath("source_inventory.json").read_bytes()
            self.assertEqual(raw_inventory, inventory_bytes(inventory))
            self.assertEqual(receipt.inventory_sha256, hashlib.sha256(raw_inventory).hexdigest())
            self.assertEqual(root.joinpath("source_inventory.sha256").read_bytes(), receipt.inventory_sha256.encode() + b"\n")
            self.assertFalse(root.joinpath(".snapshot-store.lock").exists())
            for row, snapshot in zip(inventory.snapshots, sorted(fake_81(), key=verifier.snapshot_sort_key), strict=True):
                self.assertEqual(root.joinpath(row.relative_path).read_bytes(), snapshot.canonical_bytes)

    def test_publication_hooks_prove_commit_order_and_idempotence_preserves_inodes(self):
        import mlb_backtest.v52_snapshot_store as store

        inventory = self._inventory()
        stages: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(store, "_publish_test_hook", side_effect=stages.append):
                first = self._publish(root, inventory)
            before = {path.relative_to(root): path.stat().st_ino for path in root.rglob("*") if path.is_file()}
            second = self._publish(root, inventory)
            after = {path.relative_to(root): path.stat().st_ino for path in root.rglob("*") if path.is_file()}
        self.assertEqual(
            [
                stage
                for stage in stages
                if stage in {
                    "after_blobs",
                    "after_checksum",
                    "before_inventory_link",
                    "after_inventory",
                    "before_lock_release",
                }
            ],
            ["after_blobs", "after_checksum", "before_inventory_link", "after_inventory", "before_lock_release"],
        )
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_conflicting_existing_immutable_bytes_are_never_overwritten(self):
        from mlb_backtest.v52_snapshot_store import PublishConflict

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            row = inventory.snapshots[0]
            target = root.joinpath(row.relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"wrong blob")
            with self.assertRaises(PublishConflict):
                self._publish(root, inventory)
            self.assertEqual(target.read_bytes(), b"wrong blob")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checksum = root / "source_inventory.sha256"
            checksum.write_bytes(b"wrong checksum\n")
            with self.assertRaises(PublishConflict):
                self._publish(root, inventory)
            self.assertEqual(checksum.read_bytes(), b"wrong checksum\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            final = root / "source_inventory.json"
            final.write_bytes(b"wrong inventory\n")
            with self.assertRaises(PublishConflict):
                self._publish(root, inventory)
            self.assertEqual(final.read_bytes(), b"wrong inventory\n")

    def test_existing_lock_is_held_and_unchanged(self):
        from mlb_backtest.v52_snapshot_store import LockHeld

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock = root / ".snapshot-store.lock"
            lock.write_bytes(b"other owner's lock\n")
            inode = lock.stat().st_ino
            with self.assertRaises(LockHeld):
                self._publish(root)
            self.assertEqual(lock.read_bytes(), b"other owner's lock\n")
            self.assertEqual(lock.stat().st_ino, inode)

    def test_publisher_never_uses_replace_or_network_surfaces(self):
        import mlb_backtest.v52_snapshot_store as store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(store.os, "rename", side_effect=AssertionError("rename")), patch.object(
                store.os, "replace", side_effect=AssertionError("replace")
            ), patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")), patch.object(
                socket, "socket", side_effect=AssertionError("network")
            ):
                self._publish(root)

    def test_existing_target_race_accepts_identical_and_rejects_different_bytes(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishConflict

        inventory = self._inventory()
        original_link = store.os.link
        row = inventory.snapshots[0]

        def race(with_bytes):
            tripped = False

            def link(source, destination, *args, **kwargs):
                nonlocal tripped
                if not tripped and str(destination).endswith(".snapshot.json"):
                    tripped = True
                    fd = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=kwargs["dst_dir_fd"],
                    )
                    try:
                        os.write(fd, with_bytes)
                    finally:
                        os.close(fd)
                    raise FileExistsError
                return original_link(source, destination, *args, **kwargs)

            return link

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = next(item for item in fake_81() if item.sha256 == row.snapshot_sha256)
            with patch.object(store.os, "link", side_effect=race(snapshot.canonical_bytes)):
                self._publish(root, inventory)
            self.assertEqual(root.joinpath(row.relative_path).read_bytes(), snapshot.canonical_bytes)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(store.os, "link", side_effect=race(b"attacker bytes")):
                with self.assertRaises(PublishConflict):
                    self._publish(root, inventory)
            self.assertEqual(root.joinpath(row.relative_path).read_bytes(), b"attacker bytes")

    def test_interrupted_uncommitted_publication_retries_and_after_commit_is_valid(self):
        import mlb_backtest.v52_snapshot_store as store

        inventory = self._inventory()
        for stage in ("after_blobs", "after_checksum"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                with patch.object(store, "_publish_test_hook", side_effect=lambda value: (_ for _ in ()).throw(RuntimeError(value)) if value == stage else None):
                    with self.assertRaisesRegex(RuntimeError, stage):
                        self._publish(root, inventory)
                self.assertFalse((root / "source_inventory.json").exists())
                self.assertFalse((root / ".snapshot-store.lock").exists())
                self._publish(root, inventory)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(store, "_publish_test_hook", side_effect=lambda value: (_ for _ in ()).throw(RuntimeError(value)) if value == "after_inventory" else None):
                with self.assertRaisesRegex(RuntimeError, "after_inventory"):
                    self._publish(root, inventory)
            self.assertTrue((root / "source_inventory.json").exists())
            self.assertFalse((root / ".snapshot-store.lock").exists())

    def test_mismatched_supplied_inventory_is_rejected_before_final_commit(self):
        from mlb_backtest.v52_snapshot_store import PublishError

        inventory = replace(self._inventory(), run_id="mismatched-run")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(PublishError):
                self._publish(root, inventory)
            self.assertFalse((root / "source_inventory.json").exists())

    def test_rebind_rejects_target_replaced_after_exact_read(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishConflict

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            target = root / inventory.snapshots[0].relative_path
            replacement = target.with_name("replacement.snapshot.json")
            replacement.write_bytes(target.read_bytes())
            replaced = False

            def attack(stage):
                nonlocal replaced
                if stage == "after_target_read_before_rebind" and not replaced:
                    replaced = True
                    os.rename(replacement, target)

            with patch.object(store, "_publish_test_hook", side_effect=attack):
                with self.assertRaises(PublishConflict):
                    self._publish(root, inventory)

    def test_lock_replacement_before_unlink_is_never_deleted(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock = root / ".snapshot-store.lock"
            replacement = b'{"lock_owner":"attacker","nonce":"x","run_id":"other"}\n'

            def attack(stage):
                if stage == "before_lock_unlink":
                    lock.unlink()
                    lock.write_bytes(replacement)

            with patch.object(store, "_publish_test_hook", side_effect=attack):
                with self.assertRaises(PublishError):
                    self._publish(root)
            self.assertEqual(lock.read_bytes(), replacement)

    def test_acquire_cleanup_never_deletes_lock_whose_contents_changed(self):
        import mlb_backtest.v52_snapshot_store as store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock = root / ".snapshot-store.lock"
            replacement = b'{"lock_owner":"attacker","nonce":"x","run_id":"other"}\n'

            def attack(stage):
                if stage == "after_lock_write_before_acquire_fsync":
                    lock.write_bytes(replacement)
                    raise RuntimeError("acquire failed")

            with patch.object(store, "_publish_test_hook", side_effect=attack):
                with self.assertRaisesRegex(RuntimeError, "acquire failed"):
                    self._publish(root)
            self.assertEqual(lock.read_bytes(), replacement)

    def test_swapped_source_directory_cannot_receive_publication(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        inventory = self._inventory()
        first = inventory.snapshots[0]
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            root = Path(directory).resolve()
            outside = Path(outside_directory).resolve()
            source = root / str(first.season) / first.source
            moved = outside / "moved-source"
            swapped = False

            def attack(stage):
                nonlocal swapped
                if stage == "after_directory_open_before_temp_publish" and not swapped:
                    swapped = True
                    os.rename(source, moved)
                    os.symlink(outside, source)

            with patch.object(store, "_publish_test_hook", side_effect=attack):
                with self.assertRaises(PublishError):
                    self._publish(root, inventory)
            self.assertFalse(any(outside.rglob("*.snapshot.json")))
            self.assertFalse(any(outside.rglob(".tmp-*")))

    def test_after_inventory_failure_is_exact_commit_and_only_owned_temps_are_cleaned(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import inventory_bytes

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            residue = root / ".tmp-fixture-run-prior-residue"
            residue.write_bytes(b"prior invocation")
            with patch.object(
                store,
                "_publish_test_hook",
                side_effect=lambda stage: (_ for _ in ()).throw(RuntimeError(stage))
                if stage == "after_inventory"
                else None,
            ):
                with self.assertRaisesRegex(RuntimeError, "after_inventory"):
                    self._publish(root, inventory)
            self.assertEqual((root / "source_inventory.json").read_bytes(), inventory_bytes(inventory))
            self.assertEqual(
                (root / "source_inventory.sha256").read_text(),
                hashlib.sha256(inventory_bytes(inventory)).hexdigest() + "\n",
            )
            self.assertFalse((root / ".snapshot-store.lock").exists())
            self.assertTrue(residue.exists())
            self.assertFalse(any(path.name != residue.name for path in root.rglob(".tmp-*")))
            self._publish(root, inventory)

    def test_operator_released_stale_lock_converges_linked_blob_residue(self):
        from mlb_backtest.v52_snapshot_store import LockHeld

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            target = root / inventory.snapshots[0].relative_path
            residue = target.with_name(".tmp-fixture-run-crashed-blob")
            os.link(target, residue)
            lock = root / ".snapshot-store.lock"
            lock.write_bytes(b"operator must release this stale lock\n")
            with self.assertRaises(LockHeld):
                self._publish(root, inventory)
            self.assertTrue(residue.exists())
            lock.unlink()  # Explicit operator-authorized stale-lock removal.
            self._publish(root, inventory)
            self.assertFalse(residue.exists())
            self.assertEqual(target.stat().st_nlink, 1)

    def test_operator_released_stale_lock_converges_linked_final_inventory_residue(self):
        from mlb_backtest.v52_snapshot_store import LockHeld, inventory_bytes

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            target = root / "source_inventory.json"
            residue = root / ".tmp-fixture-run-crashed-inventory"
            os.link(target, residue)
            lock = root / ".snapshot-store.lock"
            lock.write_bytes(b"operator must release this stale lock\n")
            with self.assertRaises(LockHeld):
                self._publish(root, inventory)
            lock.unlink()
            self._publish(root, inventory)
            self.assertFalse(residue.exists())
            self.assertEqual(target.read_bytes(), inventory_bytes(inventory))
            self.assertEqual(target.stat().st_nlink, 1)

    def test_linked_residue_with_wrong_run_alias_or_multiple_aliases_fails_closed(self):
        from mlb_backtest.v52_snapshot_store import PublishConflict

        inventory = self._inventory()
        for label, aliases in (
            ("wrong-run", (".tmp-other-run-crashed",)),
            ("arbitrary", ("unrelated-alias",)),
            ("multiple", (".tmp-fixture-run-first", ".tmp-fixture-run-second")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self._publish(root, inventory)
                target = root / inventory.snapshots[0].relative_path
                residue_paths = [target.with_name(alias) for alias in aliases]
                for residue in residue_paths:
                    os.link(target, residue)
                with self.assertRaises(PublishConflict):
                    self._publish(root, inventory)
                self.assertTrue(all(path.exists() for path in residue_paths))
                self.assertEqual(target.stat().st_nlink, len(aliases) + 1)

    def test_primary_and_lock_cleanup_failures_are_both_observable(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock = root / ".snapshot-store.lock"

            def fail_both(stage):
                if stage == "after_blobs":
                    raise RuntimeError("primary publication failure")
                if stage == "before_lock_unlink":
                    lock.unlink()
                    lock.write_bytes(b"replacement lock\n")

            with patch.object(store, "_publish_test_hook", side_effect=fail_both):
                with self.assertRaises(PublishError) as caught:
                    self._publish(root)
            self.assertIsInstance(caught.exception.primary_error, RuntimeError)
            self.assertIsInstance(caught.exception.cleanup_error, PublishError)
            self.assertEqual(lock.read_bytes(), b"replacement lock\n")


class LoaderTests(unittest.TestCase):
    """Read-only consumers must replay exactly the published snapshot store."""

    def test_path_adapters_reject_unexpected_root_before_filesystem_io(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            load_snapshot_store,
            publish_snapshot_store,
            read_inventory,
        )

        supplied = Path("/definitely-not-the-authorized-store")
        expected = Path("/different-authorized-store")
        with self.assertRaises(PublishError):
            read_inventory(supplied, expected_root=expected)
        with self.assertRaises(PublishError):
            load_snapshot_store(supplied, object(), expected_root=expected)  # type: ignore[arg-type]
        with self.assertRaises(PublishError):
            publish_snapshot_store(
                supplied,
                object(),
                object(),
                lock_owner="guard-test",
                expected_root=expected,
            )  # type: ignore[arg-type]

    def _publish(self, root, inventory, inputs):
        from mlb_backtest.v52_snapshot_store import publish_snapshot_store

        with patched_store_replay():
            return publish_snapshot_store(root, inventory, inputs, lock_owner="loader-test")

    @staticmethod
    def _tree_state(root):
        return {
            path.relative_to(root): (path.read_bytes(), path.stat().st_ino, path.stat().st_nlink)
            for path in root.rglob("*") if path.is_file()
        }

    def test_publish_then_load_returns_immutable_exact_replay(self):
        from mlb_backtest.v52_snapshot_store import (
            LoadedSnapshotStore,
            build_inventory,
            load_snapshot_store,
            publish_snapshot_store,
        )

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            publish_snapshot_store(root, inventory, inputs, lock_owner="loader-test")
            loaded = load_snapshot_store(root, inputs)
        self.assertIsInstance(loaded, LoadedSnapshotStore)
        self.assertEqual(loaded.inventory, inventory)
        self.assertEqual(loaded.snapshots, tuple(sorted(fake_81(), key=verifier.snapshot_sort_key)))
        with self.assertRaises(FrozenInstanceError):
            loaded.inventory = inventory  # type: ignore[misc]

    def test_publisher_verifies_with_its_owned_lock_before_release(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import build_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            original = store._load_snapshot_store_held
            calls = []

            def observe(held_root, observed_inputs, *, owned_lock):
                calls.append(owned_lock)
                return original(held_root, observed_inputs, owned_lock=owned_lock)

            with patch.object(store, "_load_snapshot_store_held", side_effect=observe):
                self._publish(root, inventory, inputs)
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], store._OwnedLock)

    def test_public_reader_rejects_any_lock_without_changing_it(self):
        from mlb_backtest.v52_snapshot_store import LockHeld, build_inventory, load_snapshot_store, read_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            lock = root / ".snapshot-store.lock"
            lock.write_bytes(b"untrusted lock\n")
            before = (lock.read_bytes(), lock.stat().st_ino)
            with self.assertRaises(LockHeld):
                read_inventory(root)
            with self.assertRaises(LockHeld):
                load_snapshot_store(root, inputs)
            self.assertEqual((lock.read_bytes(), lock.stat().st_ino), before)

    def test_read_inventory_rechecks_lock_created_at_return_boundary(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import LockHeld, build_inventory, read_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            lock = root / ".snapshot-store.lock"

            def create_lock(stage):
                if stage == "before_read_inventory_return":
                    lock.write_bytes(b"arrived during read\n")

            with patch.object(store, "_load_test_hook", side_effect=create_lock):
                with self.assertRaises(LockHeld):
                    read_inventory(root)
            self.assertEqual(lock.read_bytes(), b"arrived during read\n")

    def test_read_inventory_rejects_root_path_swapped_at_return_boundary(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, read_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "store"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)

            def swap_root(stage):
                if stage == "before_read_inventory_return":
                    os.rename(root, base / "moved-store")
                    os.rename(replacement, root)

            with patch.object(store, "_load_test_hook", side_effect=swap_root):
                with self.assertRaises(PublishError):
                    read_inventory(root)

    def test_loader_rejects_root_path_swapped_at_final_boundary(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "store"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)

            def swap_root(stage):
                if stage == "after_final_blob_reads":
                    os.rename(root, base / "moved-store")
                    os.rename(replacement, root)

            with patch.object(store, "_load_test_hook", side_effect=swap_root):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)

    def test_internal_loader_rejects_forged_owned_lock_for_other_file(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            forged_path = root / "not-the-lock"
            contents = b"forged lock capability\n"
            forged_path.write_bytes(contents)
            fd = os.open(forged_path, os.O_RDONLY)
            details = os.fstat(fd)
            forged = store._OwnedLock(
                fd, forged_path, details.st_dev, details.st_ino, "forged", contents,
                inputs.replay.run_id, "forged-owner", object(),
            )
            held = store._open_readonly_root(root)
            try:
                with self.assertRaises(PublishError):
                    store._load_snapshot_store_held(held, inputs, owned_lock=forged)
            finally:
                os.close(fd)
                store._close_directory(held)

    def test_internal_loader_rejects_wrong_issuer_for_canonical_lock(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            path = root / ".snapshot-store.lock"
            nonce = "a" * 64
            contents = json.dumps(
                {"lock_owner": "forged-owner", "nonce": nonce, "run_id": inputs.replay.run_id},
                sort_keys=True, separators=(",", ":"),
            ).encode() + b"\n"
            path.write_bytes(contents)
            fd = os.open(path, os.O_RDONLY)
            details = os.fstat(fd)
            forged = store._OwnedLock(
                fd, path, details.st_dev, details.st_ino, nonce, contents,
                inputs.replay.run_id, "forged-owner", object(),
            )
            held = store._open_readonly_root(root)
            try:
                with self.assertRaises(PublishError):
                    store._load_snapshot_store_held(held, inputs, owned_lock=forged)
                self.assertEqual(path.read_bytes(), contents)
            finally:
                os.close(fd)
                store._close_directory(held)

    def test_loader_rejects_lock_created_after_final_blob_read(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import LockHeld, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            lock = root / ".snapshot-store.lock"

            def create_lock(stage):
                if stage == "after_final_blob_reads":
                    lock.write_bytes(b"late lock\n")

            with patch.object(store, "_load_test_hook", side_effect=create_lock):
                with self.assertRaises(LockHeld):
                    load_snapshot_store(root, inputs)
            self.assertEqual(lock.read_bytes(), b"late lock\n")

    def test_loader_uses_two_replays_not_four(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            replay = tuple(sorted(fake_81(), key=verifier.snapshot_sort_key))
            with patch.object(store, "replay_verified_snapshots", return_value=replay) as called:
                load_snapshot_store(root, inputs)
            self.assertEqual(called.call_count, 2)

    def test_loader_rejects_expected_bytes_boundary_divergence(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            original = store._expected_snapshot_bytes

            def diverge(replayed, rebuilt):
                expected = original(replayed, rebuilt)
                return expected | {inventory.snapshots[0].snapshot_sha256: b"different bytes"}

            with patch.object(store, "_expected_snapshot_bytes", side_effect=diverge):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)

    def test_loaded_snapshot_payloads_are_deeply_immutable(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_source_contracts import canonical_json_bytes

        source = next(snapshot for snapshot in fake_81() if snapshot.source == "baseball_savant_statcast")
        payload = dict(source.payload)
        parent_coverage = dict(payload["parent_coverage"])
        parent_coverage["nested"] = ["mutable", {"again": ["mutable"]}]
        payload["parent_coverage"] = parent_coverage
        envelope = source.as_mapping()
        envelope["payload"] = payload
        contents = canonical_json_bytes(envelope) + b"\n"
        replacement = replace(
            source, payload=payload, canonical_bytes=contents,
            sha256=hashlib.sha256(contents).hexdigest(),
        )
        replay = tuple(sorted(
            (replacement if snapshot.sha256 == source.sha256 else snapshot for snapshot in fake_81()),
            key=verifier.snapshot_sort_key,
        ))
        with patch.object(store, "replay_verified_snapshots", return_value=replay), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = store.build_inventory(inputs)
            store.publish_snapshot_store(root, inventory, inputs, lock_owner="immutable-test")
            loaded = store.load_snapshot_store(root, inputs)
        statcast = next(snapshot for snapshot in loaded.snapshots if snapshot.source == "baseball_savant_statcast")
        parent_coverage = statcast.payload["parent_coverage"]
        with self.assertRaises(TypeError):
            parent_coverage["inclusive_start"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            parent_coverage["nested"][1]["again"] = ()  # type: ignore[index]

    def test_loader_never_calls_write_capabilities_or_changes_tree(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import build_inventory, load_snapshot_store, read_inventory

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            before = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino, path.stat().st_nlink)
                for path in root.rglob("*") if path.is_file()
            }
            original_open = store.os.open

            def readonly_open(path, flags, *args, **kwargs):
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                self.assertFalse(flags & (os.O_CREAT | os.O_EXCL | os.O_TRUNC | os.O_APPEND))
                return original_open(path, flags, *args, **kwargs)

            forbidden = AssertionError("loader attempted a write capability")
            with patch.object(store.os, "open", side_effect=readonly_open), patch.object(
                store.os, "write", side_effect=forbidden
            ), patch.object(store.os, "mkdir", side_effect=forbidden), patch.object(
                store.os, "link", side_effect=forbidden
            ), patch.object(store.os, "unlink", side_effect=forbidden), patch.object(
                store.os, "replace", side_effect=forbidden
            ), patch.object(store.os, "rename", side_effect=forbidden), patch.object(
                store.os, "fsync", side_effect=forbidden), patch.object(
                socket, "socket", side_effect=forbidden
            ):
                read = read_inventory(root)
                loaded = load_snapshot_store(root, inputs)
            self.assertEqual(read, inventory)
            self.assertEqual(loaded.inventory, inventory)
            after = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino, path.stat().st_nlink)
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_loader_rejects_bad_checksum_inventory_or_blob_without_cleanup(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            build_inventory,
            load_snapshot_store,
        )

        for target in ("source_inventory.sha256", "source_inventory.json", "blob"):
            with self.subTest(target=target), patched_store_replay(), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inputs = store_inputs()
                inventory = build_inventory(inputs)
                self._publish(root, inventory, inputs)
                path = root / (inventory.snapshots[0].relative_path if target == "blob" else target)
                path.write_bytes(b"tampered\n")
                before = path.read_bytes()
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)
                self.assertEqual(path.read_bytes(), before)

    def test_loader_rebind_rejects_same_byte_inode_replacement(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            target = root / inventory.snapshots[0].relative_path
            replacement = target.with_name("replacement.snapshot.json")
            replacement.write_bytes(target.read_bytes())

            def replace_after_reads(stage):
                if stage == "after_initial_reads":
                    os.rename(replacement, target)

            with patch.object(store, "_load_test_hook", side_effect=replace_after_reads):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)

    def test_loader_rebind_rejects_same_byte_inventory_replacement(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            target = root / "source_inventory.json"
            replacement = root / "replacement.inventory.json"
            replacement.write_bytes(target.read_bytes())

            def replace_after_reads(stage):
                if stage == "after_initial_reads":
                    os.rename(replacement, target)

            with patch.object(store, "_load_test_hook", side_effect=replace_after_reads):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)

    def test_loader_ignores_orphans_but_rejects_referenced_hardlink(self):
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            orphan = root / ".tmp-unrelated"
            orphan.write_bytes(b"orphan")
            target = root / inventory.snapshots[0].relative_path
            alias = target.with_name("untrusted-alias")
            os.link(target, alias)
            before = (orphan.read_bytes(), orphan.stat().st_ino, alias.stat().st_ino)
            with self.assertRaises(PublishError):
                load_snapshot_store(root, inputs)
            self.assertEqual((orphan.read_bytes(), orphan.stat().st_ino, alias.stat().st_ino), before)

    def test_loader_ignores_unreferenced_temp_and_orphan_blob_without_mutation(self):
        from mlb_backtest.v52_snapshot_store import build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            temp = root / ".tmp-unreferenced"
            temp.write_bytes(b"prior publisher residue")
            first = inventory.snapshots[0]
            orphan = root / str(first.season) / first.source / "orphan.snapshot.json"
            orphan.write_bytes(b"unreferenced blob")
            before = self._tree_state(root)
            loaded = load_snapshot_store(root, inputs)
            self.assertEqual(loaded.inventory, inventory)
            self.assertEqual(self._tree_state(root), before)

    def test_loader_rejects_missing_files_and_malformed_store_members_without_cleanup(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            build_inventory,
            load_snapshot_store,
            parse_inventory,
        )

        cases = (
            ("missing-checksum", "source_inventory.sha256", None),
            ("missing-inventory", "source_inventory.json", None),
            ("missing-blob", "blob", None),
            ("bad-checksum-syntax", "source_inventory.sha256", b"A" * 64 + b"\n"),
            ("bad-checksum-newline", "source_inventory.sha256", b"a" * 64),
            ("invalid-inventory", "source_inventory.json", b"{}\n"),
            ("noncanonical-inventory", "source_inventory.json", b"{ }\n"),
            ("malformed-blob", "blob", b"not json\n"),
            ("tampered-blob", "blob", b'{"changed":true}\n'),
        )
        for label, target, contents in cases:
            with self.subTest(label=label), patched_store_replay(), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inputs = store_inputs()
                inventory = build_inventory(inputs)
                self._publish(root, inventory, inputs)
                path = root / (inventory.snapshots[0].relative_path if target == "blob" else target)
                if contents is None:
                    path.unlink()
                    expected = False
                else:
                    path.write_bytes(contents)
                    expected = contents
                    if target == "source_inventory.json":
                        (root / "source_inventory.sha256").write_bytes(
                            hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n"
                        )
                        with self.assertRaises(ValueError):
                            parse_inventory(contents)
                before = self._tree_state(root)
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)
                self.assertEqual(path.exists(), expected is not False)
                if expected is not False:
                    self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(self._tree_state(root), before)

    def test_loader_rejects_wrong_authorities_run_id_and_phase_without_mutation(self):
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        binding_names = (
            "run_spec", "acquisition_contract", "terminal_checkpoint", "schedule_inventory",
            "game_evidence_inventory", "timezone_gate",
        )
        for label in ("run_id", *binding_names, "phase"):
            with self.subTest(label=label), patched_store_replay(), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inputs = store_inputs()
                inventory = build_inventory(inputs)
                self._publish(root, inventory, inputs)
                if label == "run_id":
                    bad_inputs = replace(inputs, replay=replace(inputs.replay, run_id="wrong-run"))
                elif label == "phase":
                    bad_inputs = replace(inputs, current_phase="TERMINAL")
                else:
                    binding = getattr(inputs.bindings, label)
                    bad_bindings = replace(inputs.bindings, **{label: replace(binding, sha256="0" * 64)})
                    bad_inputs = replace(inputs, bindings=bad_bindings)
                before = self._tree_state(root)
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, bad_inputs)
                self.assertEqual(self._tree_state(root), before)

    def test_loader_rejects_replay_exception_and_mismatch_without_mutation(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        for label, failure, expected in (
            ("exception", RuntimeError("replay failed"), RuntimeError),
            ("mismatch", tuple(reversed(fake_81())), PublishError),
        ):
            with self.subTest(label=label), patched_store_replay(), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inputs = store_inputs()
                inventory = build_inventory(inputs)
                self._publish(root, inventory, inputs)
                before = self._tree_state(root)
                with patch.object(store, "replay_verified_snapshots", side_effect=failure):
                    with self.assertRaises(expected):
                        load_snapshot_store(root, inputs)
                self.assertEqual(self._tree_state(root), before)

    def test_loader_rejects_replay_mutated_between_first_and_second_replay(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, build_inventory, load_snapshot_store

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            before = self._tree_state(root)
            changed = False
            stable = tuple(sorted(fake_81(), key=verifier.snapshot_sort_key))

            def mutate_at_boundary(stage):
                nonlocal changed
                if stage == "between_replays":
                    changed = True

            def replay(_inputs):
                return tuple(reversed(stable)) if changed else stable

            with patch.object(store, "_load_test_hook", side_effect=mutate_at_boundary), patch.object(
                store, "replay_verified_snapshots", side_effect=replay
            ):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, inputs)
            self.assertTrue(changed)
            self.assertEqual(self._tree_state(root), before)

    def test_loader_rejects_checksum_valid_same_season_statcast_permutation(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            build_inventory,
            load_snapshot_store,
            parse_inventory,
        )

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            raw = json.loads((root / "source_inventory.json").read_bytes())
            indexes = [
                index for index, row in enumerate(raw["snapshots"])
                if row["season"] == 2018 and row["source"] == "baseball_savant_statcast"
            ]
            self.assertGreater(len(indexes), 1)
            reordered = list(reversed([raw["snapshots"][index] for index in indexes]))
            for index, row in zip(indexes, reordered, strict=True):
                raw["snapshots"][index] = row
            raw["snapshot_sha256s"] = [row["snapshot_sha256"] for row in raw["snapshots"]]
            source_season = next(
                row for row in raw["source_seasons"]
                if row["season"] == 2018 and row["source"] == "baseball_savant_statcast"
            )
            source_season["snapshot_sha256s"] = [row["snapshot_sha256"] for row in reordered]
            tampered = _canonical_raw(raw)
            parse_inventory(tampered)
            (root / "source_inventory.json").write_bytes(tampered)
            (root / "source_inventory.sha256").write_bytes(
                hashlib.sha256(tampered).hexdigest().encode() + b"\n"
            )
            with self.assertRaises(PublishError):
                load_snapshot_store(root, inputs)


class FdLoaderTests(unittest.TestCase):
    """Capability-rooted loads never reopen the snapshot-store by pathname."""

    @staticmethod
    def _publish(root, inventory, inputs):
        from mlb_backtest.v52_snapshot_store import publish_snapshot_store

        with patched_store_replay():
            publish_snapshot_store(root, inventory, inputs, lock_owner="fd-loader-test")

    def test_fd_loader_returns_exact_store_and_preserves_caller_fd(self):
        from mlb_backtest.v52_snapshot_store import (
            build_inventory,
            load_snapshot_store_from_fd,
        )

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            descriptor = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
            try:
                loaded = load_snapshot_store_from_fd(descriptor, inputs)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        self.assertEqual(loaded.inventory, inventory)
        self.assertEqual(
            (after.st_dev, after.st_ino),
            (before.st_dev, before.st_ino),
        )

    def test_fd_loader_rejects_tampered_inventory(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            build_inventory,
            load_snapshot_store_from_fd,
        )

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            (root / "source_inventory.json").write_bytes(b"{}\n")
            descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with self.assertRaises(PublishError):
                    load_snapshot_store_from_fd(descriptor, inputs)
                os.fstat(descriptor)
            finally:
                os.close(descriptor)

    def test_fd_loader_rejects_aliased_and_special_inventory_leaf(self):
        from mlb_backtest.v52_snapshot_store import (
            PublishError,
            build_inventory,
            load_snapshot_store_from_fd,
        )

        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind), patched_store_replay(), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inputs = store_inputs()
                inventory = build_inventory(inputs)
                self._publish(root, inventory, inputs)
                target = root / "source_inventory.json"
                raw = target.read_bytes()
                target.unlink()
                external = root / f"{kind}-external"
                external.write_bytes(raw)
                if kind == "symlink":
                    target.symlink_to(external)
                elif kind == "hardlink":
                    os.link(external, target)
                else:
                    os.mkfifo(target)
                descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    with self.assertRaises(PublishError):
                        load_snapshot_store_from_fd(descriptor, inputs)
                    os.fstat(descriptor)
                finally:
                    os.close(descriptor)

    def test_fd_loader_never_uses_write_capabilities(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import (
            build_inventory,
            load_snapshot_store_from_fd,
        )

        with patched_store_replay(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = store_inputs()
            inventory = build_inventory(inputs)
            self._publish(root, inventory, inputs)
            descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with (
                    patch.object(store.os, "write", side_effect=AssertionError("write")) as write,
                    patch.object(store.os, "mkdir", side_effect=AssertionError("mkdir")) as mkdir,
                    patched_store_replay(),
                ):
                    loaded = load_snapshot_store_from_fd(descriptor, inputs)
                os.fstat(descriptor)
            finally:
                os.close(descriptor)
        self.assertEqual(loaded.inventory, inventory)
        write.assert_not_called()
        mkdir.assert_not_called()


class AttackTests(unittest.TestCase):
    """Filesystem adversary cases for authoritative snapshot-store names."""

    def _inventory(self):
        from mlb_backtest.v52_snapshot_store import build_inventory

        with patched_store_replay():
            return build_inventory(store_inputs())

    def _publish(self, root, inventory):
        from mlb_backtest.v52_snapshot_store import publish_snapshot_store

        with patched_store_replay():
            return publish_snapshot_store(
                root, inventory, store_inputs(), lock_owner="attack-test"
            )

    def test_path_publish_rejects_regular_ancestor_swap_without_external_write(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "store"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            canary = replacement / "must-not-change"
            canary.write_bytes(b"external\n")

            def swap(stage):
                if stage == "before_path_publish_return":
                    os.rename(root, base / "moved-store")
                    os.rename(replacement, root)

            with patch.object(store, "_publish_test_hook", side_effect=swap):
                with self.assertRaises(PublishError):
                    self._publish(root, inventory)
            self.assertEqual((root / "must-not-change").read_bytes(), b"external\n")

    def test_path_read_rejects_symlink_ancestor_swap(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, read_inventory

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "store"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            canary = replacement / "must-not-change"
            canary.write_bytes(b"external\n")
            self._publish(root, inventory)

            def swap(stage):
                if stage == "before_read_inventory_return":
                    os.rename(root, base / "moved-store")
                    os.symlink(replacement, root)

            with patch.object(store, "_load_test_hook", side_effect=swap):
                with self.assertRaises(PublishError):
                    read_inventory(root)
            self.assertEqual(canary.read_bytes(), b"external\n")

    def test_path_load_rejects_root_replacement_before_final_return(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, load_snapshot_store

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "store"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            self._publish(root, inventory)

            def swap(stage):
                if stage == "before_path_load_return":
                    os.rename(root, base / "moved-store")
                    os.rename(replacement, root)

            with patch.object(store, "_load_test_hook", side_effect=swap):
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, store_inputs())

    def test_path_load_rechecks_lock_created_at_return_boundary_without_fd_leak(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import LockHeld, load_snapshot_store

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            lock = root / ".snapshot-store.lock"
            fd_directory = Path("/dev/fd")
            if not fd_directory.is_dir():
                self.skipTest("platform does not expose /dev/fd")

            def create_lock(stage):
                if stage == "before_path_load_return":
                    lock.write_bytes(b"arrived during load\n")

            before = set(os.listdir(fd_directory))
            with patched_store_replay(), patch.object(
                store, "_load_test_hook", side_effect=create_lock
            ):
                with self.assertRaises(LockHeld):
                    load_snapshot_store(root, store_inputs())
            self.assertEqual(set(os.listdir(fd_directory)), before)
            self.assertEqual(lock.read_bytes(), b"arrived during load\n")

    def test_path_adapters_close_capability_fds_on_failures(self):
        from mlb_backtest.v52_snapshot_store import LockHeld, PublishError, read_inventory

        fd_directory = Path("/dev/fd")
        if not fd_directory.is_dir():
            self.skipTest("platform does not expose /dev/fd")
        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = set(os.listdir(fd_directory))
            with self.assertRaises(PublishError):
                read_inventory(root)
            self.assertEqual(set(os.listdir(fd_directory)), before)

            (root / ".snapshot-store.lock").write_bytes(b"foreign lock\n")
            before = set(os.listdir(fd_directory))
            with self.assertRaises(LockHeld):
                self._publish(root, inventory)
            self.assertEqual(set(os.listdir(fd_directory)), before)

    def test_publisher_rejects_special_authoritative_leaves_without_opening_or_mutating_them(self):
        """A FIFO must be refused by metadata, never opened for a blocking read."""
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import LockHeld, SnapshotStoreError

        if not hasattr(os, "mkfifo"):
            self.skipTest("platform does not provide POSIX FIFOs")
        inventory = self._inventory()
        leaves = (
            ("blob", inventory.snapshots[0].relative_path),
            ("checksum", "source_inventory.sha256"),
            ("inventory", "source_inventory.json"),
            ("lock", ".snapshot-store.lock"),
        )
        for leaf_label, relative in leaves:
            for kind in ("symlink", "hardlink", "fifo", "directory"):
                with self.subTest(leaf=leaf_label, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    outside = root / "outside"
                    outside.mkdir()
                    external = outside / "external-bytes"
                    external.write_bytes(b"external bytes must not change")
                    if kind == "symlink":
                        os.symlink(external, target)
                    elif kind == "hardlink":
                        os.link(external, target)
                    elif kind == "fifo":
                        os.mkfifo(target)
                    elif kind == "directory":
                        target.mkdir()
                    before_external = external.read_bytes()
                    original_open = store.os.open

                    def refuse_fifo_open(path, flags, *args, **kwargs):
                        if kind == "fifo" and leaf_label != "lock" and path == target.name:
                            raise AssertionError("publisher attempted to open FIFO leaf")
                        return original_open(path, flags, *args, **kwargs)

                    expected = LockHeld if leaf_label == "lock" else SnapshotStoreError
                    with patch.object(store.os, "open", side_effect=refuse_fifo_open):
                        with self.assertRaises(expected):
                            self._publish(root, inventory)
                    self.assertEqual(external.read_bytes(), before_external)
                    self.assertTrue(os.path.lexists(target))

    def test_loader_rejects_fifo_authoritative_leaves_without_opening_or_cleaning_them(self):
        """The read-only path has the same non-blocking special-file invariant."""
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import LockHeld, PublishError, load_snapshot_store

        if not hasattr(os, "mkfifo"):
            self.skipTest("platform does not provide POSIX FIFOs")
        inventory = self._inventory()
        leaves = (
            ("blob", inventory.snapshots[0].relative_path),
            ("checksum", "source_inventory.sha256"),
            ("inventory", "source_inventory.json"),
            ("lock", ".snapshot-store.lock"),
        )
        for leaf_label, relative in leaves:
            with self.subTest(leaf=leaf_label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self._publish(root, inventory)
                target = root / relative
                if target.exists():
                    target.unlink()
                os.mkfifo(target)
                original_open = store.os.open

                def refuse_fifo_open(path, flags, *args, **kwargs):
                    if leaf_label != "lock" and path == target.name:
                        raise AssertionError("loader attempted to open FIFO leaf")
                    return original_open(path, flags, *args, **kwargs)

                expected = LockHeld if leaf_label == "lock" else PublishError
                with patch.object(store.os, "open", side_effect=refuse_fifo_open):
                    with self.assertRaises(expected):
                        load_snapshot_store(root, store_inputs())
                self.assertTrue(os.path.lexists(target))

    def test_loader_rejects_symlink_hardlink_and_directory_authoritative_leaves_without_cleanup(self):
        from mlb_backtest.v52_snapshot_store import LockHeld, PublishError, load_snapshot_store

        inventory = self._inventory()
        leaves = (
            ("blob", inventory.snapshots[0].relative_path),
            ("checksum", "source_inventory.sha256"),
            ("inventory", "source_inventory.json"),
            ("lock", ".snapshot-store.lock"),
        )
        for leaf_label, relative in leaves:
            for kind in ("symlink", "hardlink", "directory"):
                with self.subTest(leaf=leaf_label, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    self._publish(root, inventory)
                    target = root / relative
                    if target.exists():
                        target.unlink()
                    outside = root / "outside"
                    outside.mkdir()
                    external = outside / "external-bytes"
                    external.write_bytes(b"external bytes must not change")
                    if kind == "symlink":
                        os.symlink(external, target)
                    elif kind == "hardlink":
                        os.link(external, target)
                    else:
                        target.mkdir()
                    before_external = external.read_bytes()
                    expected = LockHeld if leaf_label == "lock" else PublishError
                    with self.assertRaises(expected):
                        load_snapshot_store(root, store_inputs())
                    self.assertEqual(external.read_bytes(), before_external)
                    self.assertTrue(os.path.lexists(target))

    def test_unix_socket_authoritative_root_leaves_are_rejected_without_cleanup(self):
        from mlb_backtest.v52_snapshot_store import LockHeld, PublishError, SnapshotStoreError, load_snapshot_store

        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("platform does not provide Unix-domain sockets")
        inventory = self._inventory()
        for leaf_label, relative in (
            ("checksum", "source_inventory.sha256"),
            ("inventory", "source_inventory.json"),
            ("lock", ".snapshot-store.lock"),
        ):
            with self.subTest(mode="publisher", leaf=leaf_label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                target = root / relative
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(target))
                try:
                    expected = LockHeld if leaf_label == "lock" else SnapshotStoreError
                    with self.assertRaises(expected):
                        self._publish(root, inventory)
                    self.assertTrue(os.path.lexists(target))
                finally:
                    listener.close()
            with self.subTest(mode="loader", leaf=leaf_label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self._publish(root, inventory)
                target = root / relative
                if target.exists():
                    target.unlink()
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(target))
                try:
                    expected = LockHeld if leaf_label == "lock" else PublishError
                    with self.assertRaises(expected):
                        load_snapshot_store(root, store_inputs())
                    self.assertTrue(os.path.lexists(target))
                finally:
                    listener.close()

    def test_snapshot_blob_unix_socket_substitution_rejects_publisher_and_loader(self):
        from mlb_backtest.v52_snapshot_store import PublishError, SnapshotStoreError, load_snapshot_store

        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("platform does not provide Unix-domain sockets")
        inventory = self._inventory()
        for mode in ("publisher", "loader"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir="/tmp", prefix="v52-") as directory:
                root = Path(directory).resolve()
                self.assertTrue(root.is_absolute())
                target = root / inventory.snapshots[0].relative_path
                if mode == "publisher":
                    target.parent.mkdir(parents=True)
                else:
                    self._publish(root, inventory)
                    target.unlink()
                staging = root / ".socket-staging"
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    try:
                        listener.bind(str(staging))
                    except OSError as exc:
                        if exc.errno == errno.ENAMETOOLONG:
                            self.skipTest("platform sockaddr_un path limit prevents blob socket setup")
                        raise
                    os.rename(staging, target)
                    before = os.lstat(target)
                    if mode == "publisher":
                        with self.assertRaises(SnapshotStoreError):
                            self._publish(root, inventory)
                    else:
                        with patched_store_replay():
                            with self.assertRaises(PublishError):
                                load_snapshot_store(root, store_inputs())
                    after = os.lstat(target)
                    self.assertEqual((after.st_dev, after.st_ino, after.st_mode), (
                        before.st_dev, before.st_ino, before.st_mode,
                    ))
                finally:
                    listener.close()

    def test_publisher_rebinds_root_season_and_source_before_writing_after_path_replacement(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        inventory = self._inventory()
        first = inventory.snapshots[0]
        for target_kind in ("root", "season", "source"):
            for replacement_kind in ("symlink", "different-directory"):
                with self.subTest(target=target_kind, replacement=replacement_kind), tempfile.TemporaryDirectory() as directory:
                    container = Path(directory).resolve()
                    root = container / "root"
                    root.mkdir()
                    outside = container / "outside"
                    outside.mkdir()
                    swapped = False
                    outside_state = None

                    def attacker_visible_state():
                        return {
                            relative: (path.read_bytes(), path.stat().st_ino)
                            for path in outside.rglob("*")
                            if path.is_file()
                            for relative in (path.relative_to(outside),)
                            if not (
                                target_kind == "root"
                                and relative.parts[0] == "moved-root"
                            )
                        }

                    def attack(stage):
                        nonlocal swapped, outside_state
                        if stage != "after_directory_open_before_temp_publish" or swapped:
                            return
                        swapped = True
                        target = {
                            "root": root,
                            "season": root / str(first.season),
                            "source": root / str(first.season) / first.source,
                        }[target_kind]
                        moved = outside / f"moved-{target_kind}"
                        os.rename(target, moved)
                        if replacement_kind == "symlink":
                            os.symlink(outside, target)
                        else:
                            target.mkdir()
                        outside_state = attacker_visible_state()

                    with patch.object(store, "_publish_test_hook", side_effect=attack):
                        with self.assertRaises(PublishError):
                            self._publish(root, inventory)
                    self.assertTrue(swapped)
                    self.assertEqual(attacker_visible_state(), outside_state)
                    self.assertTrue(os.path.lexists({
                        "root": root,
                        "season": root / str(first.season),
                        "source": root / str(first.season) / first.source,
                    }[target_kind]))

    def test_loader_rebinds_root_season_and_source_after_held_directory_open(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, load_snapshot_store

        inventory = self._inventory()
        first = inventory.snapshots[0]
        for target_kind in ("root", "season", "source"):
            for replacement_kind in ("symlink", "different-directory"):
                with self.subTest(target=target_kind, replacement=replacement_kind), tempfile.TemporaryDirectory() as directory:
                    container = Path(directory).resolve()
                    root = container / "root"
                    root.mkdir()
                    self._publish(root, inventory)
                    outside = container / "outside"
                    outside.mkdir()
                    swapped = False
                    outside_state = None

                    def attack(stage):
                        nonlocal swapped, outside_state
                        if stage != "after_initial_reads" or swapped:
                            return
                        swapped = True
                        target = {
                            "root": root,
                            "season": root / str(first.season),
                            "source": root / str(first.season) / first.source,
                        }[target_kind]
                        moved = outside / f"moved-{target_kind}"
                        os.rename(target, moved)
                        if replacement_kind == "symlink":
                            os.symlink(outside, target)
                        else:
                            target.mkdir()
                        outside_state = {
                            path.relative_to(outside): (path.read_bytes(), path.stat().st_ino)
                            for path in outside.rglob("*") if path.is_file()
                        }

                    with patch.object(store, "_load_test_hook", side_effect=attack):
                        with self.assertRaises(PublishError):
                            load_snapshot_store(root, store_inputs())
                    self.assertTrue(swapped)
                    self.assertEqual(
                        {
                            path.relative_to(outside): (path.read_bytes(), path.stat().st_ino)
                            for path in outside.rglob("*") if path.is_file()
                        },
                        outside_state,
                    )

    def test_parser_rejects_all_path_escape_components_without_filesystem_access(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import inventory_bytes, parse_inventory

        raw = inventory_bytes(self._inventory())
        mutations = (
            ("binding-absolute", "run_spec", "/outside/run_spec.json"),
            ("binding-parent", "run_spec", "audit/../outside.json"),
            ("binding-empty", "run_spec", "audit//outside.json"),
            ("binding-dot", "run_spec", "audit/./outside.json"),
            ("binding-backslash", "run_spec", "audit\\outside.json"),
            ("binding-control", "run_spec", "audit/\x7foutside.json"),
            ("snapshot-absolute", "snapshot", "/outside.snapshot.json"),
            ("snapshot-parent", "snapshot", "../outside.snapshot.json"),
            ("snapshot-empty", "snapshot", "2018//outside.snapshot.json"),
            ("snapshot-dot", "snapshot", "2018/./outside.snapshot.json"),
            ("snapshot-backslash", "snapshot", "2018\\outside.snapshot.json"),
            ("snapshot-control", "snapshot", "2018/\x00outside.snapshot.json"),
        )
        for label, target, path in mutations:
            with self.subTest(label=label):
                candidate = json.loads(raw)
                if target == "snapshot":
                    candidate["snapshots"][0]["relative_path"] = path
                else:
                    candidate[target]["path"] = path
                with patch.object(store.os, "open", side_effect=AssertionError("parser opened a path")):
                    with self.assertRaises((TypeError, ValueError)):
                        parse_inventory(_canonical_raw(candidate))
        for label, path in (
            ("season-mismatch", "2019/baseball_savant_statcast/"),
            ("source-mismatch", "2018/mlb_stats_pitching/"),
        ):
            with self.subTest(label=label):
                candidate = json.loads(raw)
                row = candidate["snapshots"][0]
                row["relative_path"] = path + row["snapshot_sha256"] + ".snapshot.json"
                with self.assertRaises((TypeError, ValueError)):
                    parse_inventory(_canonical_raw(candidate))

    def test_concurrent_checksum_and_inventory_links_accept_exact_bytes_and_reject_conflicts(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishConflict, inventory_bytes

        inventory = self._inventory()
        expected_by_name = {
            "source_inventory.sha256": hashlib.sha256(inventory_bytes(inventory)).hexdigest().encode() + b"\n",
            "source_inventory.json": inventory_bytes(inventory),
        }
        for target_name, expected_bytes in expected_by_name.items():
            for race_bytes, error in ((expected_bytes, None), (b"concurrent attacker bytes\n", PublishConflict)):
                with self.subTest(target=target_name, identical=error is None), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    original_link = store.os.link
                    tripped = False

                    def race(source, destination, *args, **kwargs):
                        nonlocal tripped
                        if not tripped and destination == target_name:
                            tripped = True
                            fd = os.open(
                                destination,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                0o600,
                                dir_fd=kwargs["dst_dir_fd"],
                            )
                            try:
                                os.write(fd, race_bytes)
                            finally:
                                os.close(fd)
                            raise FileExistsError
                        return original_link(source, destination, *args, **kwargs)

                    with patch.object(store.os, "link", side_effect=race):
                        if error is None:
                            self._publish(root, inventory)
                        else:
                            with self.assertRaises(error):
                                self._publish(root, inventory)
                    self.assertTrue(tripped)
                    self.assertEqual((root / target_name).read_bytes(), race_bytes)

    def test_crash_boundaries_require_operator_lock_handling_before_identical_retry(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError

        inventory = self._inventory()
        for stage in ("after_blobs", "after_checksum", "before_inventory_link"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()

                def interrupt(current):
                    if current == stage:
                        raise RuntimeError(stage)

                def leave_lock(lock, _root):
                    os.close(lock.fd)
                    raise PublishError("simulated crash left lock")

                with patch.object(store, "_publish_test_hook", side_effect=interrupt), patch.object(
                    store, "_release_lock", side_effect=leave_lock
                ):
                    with self.assertRaises(PublishError):
                        self._publish(root, inventory)
                lock = root / ".snapshot-store.lock"
                self.assertTrue(lock.exists())
                self.assertFalse((root / "source_inventory.json").exists())
                self.assertEqual(
                    (root / "source_inventory.sha256").exists(), stage != "after_blobs"
                )
                lock.unlink()  # Explicit operator-authorized stale-lock removal.
                self._publish(root, inventory)

    def test_operator_released_stale_lock_converges_linked_checksum_residue(self):
        from mlb_backtest.v52_snapshot_store import LockHeld

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            target = root / "source_inventory.sha256"
            residue = root / ".tmp-fixture-run-crashed-checksum"
            os.link(target, residue)
            lock = root / ".snapshot-store.lock"
            lock.write_bytes(b"operator must release this stale lock\n")
            with self.assertRaises(LockHeld):
                self._publish(root, inventory)
            self.assertTrue(residue.exists())
            lock.unlink()  # Explicit operator-authorized stale-lock removal.
            self._publish(root, inventory)
            self.assertFalse(residue.exists())
            self.assertEqual(target.stat().st_nlink, 1)

    def test_crash_after_inventory_leaves_exact_loadable_commit_after_operator_lock_handling(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, inventory_bytes, load_snapshot_store

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            def interrupt(stage):
                if stage == "after_inventory":
                    raise RuntimeError(stage)

            def leave_lock(lock, _root):
                os.close(lock.fd)
                raise PublishError("simulated crash left lock")

            with patch.object(store, "_publish_test_hook", side_effect=interrupt), patch.object(
                store, "_release_lock", side_effect=leave_lock
            ):
                with self.assertRaises(PublishError):
                    self._publish(root, inventory)
            self.assertEqual((root / "source_inventory.json").read_bytes(), inventory_bytes(inventory))
            lock = root / ".snapshot-store.lock"
            self.assertTrue(lock.exists())
            lock.unlink()  # Explicit operator-authorized stale-lock removal.
            with patched_store_replay():
                loaded = load_snapshot_store(root, store_inputs())
            self.assertEqual(loaded.inventory, inventory)

    def test_same_store_loads_identically_in_both_allowed_phases_without_mutation(self):
        from mlb_backtest.v52_snapshot_store import inventory_bytes, load_snapshot_store, read_inventory

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            before = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino, path.stat().st_nlink)
                for path in root.rglob("*") if path.is_file()
            }
            acquiring = store_inputs(current_phase="ACQUIRING")
            sources_ready = store_inputs(current_phase="SOURCES_READY")
            with patched_store_replay():
                acquired = load_snapshot_store(root, acquiring)
                ready = load_snapshot_store(root, sources_ready)
            self.assertEqual(acquired.inventory, ready.inventory)
            self.assertEqual(inventory_bytes(acquired.inventory), inventory_bytes(ready.inventory))
            self.assertEqual(read_inventory(root), inventory)
            after = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino, path.stat().st_nlink)
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_leaf_replacement_between_metadata_check_and_open_uses_nonblocking_read(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, load_snapshot_store

        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("platform does not provide POSIX FIFO nonblocking opens")
        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            target = root / inventory.snapshots[0].relative_path
            original_open = store.os.open
            swapped = False

            def swap_before_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if not swapped and path == target.name:
                    swapped = True
                    if not flags & os.O_NONBLOCK:
                        raise AssertionError("immutable reader opened a raced FIFO without O_NONBLOCK")
                    target.unlink()
                    os.mkfifo(target)
                return original_open(path, flags, *args, **kwargs)

            with patch.object(store.os, "open", side_effect=swap_before_open), patched_store_replay():
                with self.assertRaises(PublishError):
                    load_snapshot_store(root, store_inputs())
            self.assertTrue(swapped)
            self.assertTrue(os.path.lexists(target))

    def test_missing_nonblocking_capability_fails_before_opening_an_authoritative_leaf(self):
        import mlb_backtest.v52_snapshot_store as store
        from mlb_backtest.v52_snapshot_store import PublishError, load_snapshot_store

        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._publish(root, inventory)
            original_open = store.os.open

            def permit_directories_only(path, flags, *args, **kwargs):
                if path in {"source_inventory.sha256", "source_inventory.json"}:
                    raise AssertionError("opened authoritative leaf without O_NONBLOCK capability")
                return original_open(path, flags, *args, **kwargs)

            with patch.object(store, "_OPEN_READONLY", None), patch.object(
                store.os, "open", side_effect=permit_directories_only
            ):
                with self.assertRaisesRegex(PublishError, "O_NONBLOCK"):
                    load_snapshot_store(root, store_inputs())


class EvidenceTests(unittest.TestCase):
    @staticmethod
    def _reports() -> Path:
        return Path(__file__).resolve().parents[1] / "reports" / "v5_2_qd"

    def test_publish_evidence_has_exact_closed_keys(self):
        reports = self._reports()
        value = json.loads(
            (reports / "source_snapshot_store_publish.json").read_bytes()
        )
        expected = {
            "evidence_scope",
            "canonical_historical_publish_performed",
            "run_id",
            "inventory_sha256",
            "snapshot_count",
            "source_season_count",
            "source_count",
            "inventory_status",
            "by_source",
            "by_season",
            "commands",
            "protected_duplicates_untouched",
        }
        self.assertEqual(set(value), expected)
        self.assertEqual(value["evidence_scope"], "HERMETIC_CONTRACT_FIXTURE")
        self.assertFalse(value["canonical_historical_publish_performed"])
        self.assertEqual(
            (value["snapshot_count"], value["source_season_count"], value["source_count"]),
            (81, 42, 6),
        )
        self.assertEqual(value["inventory_status"], "READY_CANDIDATE")
        self.assertRegex(value["inventory_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(value["protected_duplicates_untouched"])

    def test_publish_evidence_values_are_canonical_and_disclosed(self):
        from mlb_backtest import v52_snapshot_store as store
        from mlb_backtest.v52_source_contracts import canonical_json_bytes

        report = self._reports() / "source_snapshot_store_publish.json"
        raw = report.read_bytes()
        value = json.loads(raw)
        self.assertEqual(raw, canonical_json_bytes(value) + b"\n")
        with tempfile.TemporaryDirectory() as directory, patched_store_replay():
            inputs = store_inputs()
            inventory = store.build_inventory(inputs)
            receipt = store.publish_snapshot_store(
                Path(directory).resolve(), inventory, inputs, lock_owner="evidence-test"
            )
        self.assertEqual(value["run_id"], "fixture-run")
        self.assertEqual(value["inventory_sha256"], receipt.inventory_sha256)
        self.assertEqual(
            value["by_source"],
            {
                "baseball_savant_statcast": 46,
                "mlb_schedule_game_feed": 7,
                "mlb_stats_pitcher_bf": 7,
                "mlb_stats_pitching": 7,
                "prior_schedule_history": 7,
                "venue_team_timezone_table": 7,
            },
        )
        self.assertEqual(
            value["by_season"],
            {"2018": 12, "2019": 12, "2020": 8, "2021": 12, "2022": 12, "2023": 13, "2024": 12},
        )
        self.assertEqual(
            value["commands"],
            [
                "python3 -m unittest discover -s tests -p 'test*.py' -v",
                "ruff check mlb_backtest tests",
            ],
        )
        markdown = (self._reports() / "source_snapshot_store_publish.md").read_text()
        self.assertIn("canonical historical publish was NOT performed", markdown)
        self.assertIn("Real publish remains blocked until actual sealed SnapshotStoreInputs exist", markdown)
        self.assertIn(receipt.inventory_sha256, markdown)
        self.assertIn("81 snapshots, 42 source-season pairs, 6 sources", markdown)

    def test_protected_duplicate_status_requires_exact_four_untracked_entries(self):
        import tests.write_v52_snapshot_store_contract_evidence as writer

        paths = sorted(writer.PROTECTED_DUPLICATES)

        def raw(entries):
            return b"".join(
                f"{status} {path}".encode() + b"\0"
                for status, path in entries
            )

        expected = [("??", path) for path in paths]
        self.assertTrue(writer._protected_status_is_expected(raw(expected)))
        self.assertFalse(writer._protected_status_is_expected(raw(expected[:-1])))
        self.assertFalse(writer._protected_status_is_expected(b""))
        for status in ("M ", " M", "D ", " D", "R "):
            with self.subTest(status=status):
                self.assertFalse(
                    writer._protected_status_is_expected(
                        raw([(status, path) for path in paths])
                    )
                )
        self.assertFalse(
            writer._protected_status_is_expected(
                raw(expected + [("??", "extra/unexpected.txt")])
            )
        )

    def test_evidence_writer_is_hermetic_and_does_not_advance_phase(self):
        import socket
        import urllib.request

        import tests.write_v52_snapshot_store_contract_evidence as writer

        canonical = writer.CANONICAL_SOURCES
        self.assertFalse(os.path.lexists(canonical))
        phases: list[str] = []
        original_store_inputs = writer.fixture.store_inputs

        def capture_inputs(*args, **kwargs):
            inputs = original_store_inputs(*args, **kwargs)
            phases.append(inputs.current_phase)
            return inputs

        with patch.object(writer.fixture, "store_inputs", side_effect=capture_inputs), patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("network")
        ), patch.object(socket, "socket", side_effect=AssertionError("network")):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve()
                generated_json, generated_markdown = writer.generate_to(output)
                self.assertEqual(
                    generated_json,
                    (self._reports() / "source_snapshot_store_publish.json").read_bytes(),
                )
                self.assertEqual(
                    generated_markdown,
                    (self._reports() / "source_snapshot_store_publish.md").read_bytes(),
                )
                self.assertEqual(
                    (output / "source_snapshot_store_publish.json").read_bytes(),
                    generated_json,
                )
                self.assertEqual(
                    (output / "source_snapshot_store_publish.md").read_bytes(),
                    generated_markdown,
                )
                self.assertFalse(
                    any(path.name.startswith(".") for path in output.iterdir())
                )
        self.assertFalse(os.path.lexists(canonical))
        self.assertEqual(phases, ["SOURCES_READY"])
        self.assertFalse(
            json.loads(
                (self._reports() / "source_snapshot_store_publish.json").read_bytes()
            )["canonical_historical_publish_performed"]
        )

    def test_atomic_report_write_cleans_owned_temp_on_replace_failure(self):
        import tests.write_v52_snapshot_store_contract_evidence as writer

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            with patch.object(writer.os, "replace", side_effect=RuntimeError("replace")):
                with self.assertRaisesRegex(RuntimeError, "replace"):
                    writer._atomic_write(target, b"report")
            self.assertFalse(target.exists())
            self.assertEqual(tuple(Path(directory).iterdir()), ())

    def test_generation_rejects_canonical_historical_output_path(self):
        import tests.write_v52_snapshot_store_contract_evidence as writer

        self.assertFalse(os.path.lexists(writer.CANONICAL_SOURCES))
        with self.assertRaisesRegex(AssertionError, "canonical historical sources"):
            writer.generate_to(writer.CANONICAL_SOURCES)
        self.assertFalse(os.path.lexists(writer.CANONICAL_SOURCES))

    def test_git_status_uses_sanitized_environment_and_exact_repository(self):
        import tests.write_v52_snapshot_store_contract_evidence as writer

        contaminated = {
            "GIT_DIR": "/tmp/not-this-repository",
            "GIT_WORK_TREE": "/tmp/not-this-tree",
            "GIT_INDEX_FILE": "/tmp/not-this-index",
            "GIT_COMMON_DIR": "/tmp/not-this-common",
            "GIT_OBJECT_DIRECTORY": "/tmp/not-this-objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/not-this-alternates",
        }
        with patch.dict(os.environ, contaminated, clear=False):
            self.assertTrue(writer._protected_duplicates_untouched())
            sanitized = writer._sanitized_git_env()
        self.assertTrue(all(key not in sanitized for key in contaminated))


if __name__ == "__main__":
    unittest.main()


def _canonical_raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _raw_with_two_split_receipts(raw):
    value = json.loads(raw)
    first, second = value["snapshots"][:2]
    first["split_receipt_sha256s"] = ["d" * 64]
    second["split_receipt_sha256s"] = ["e" * 64]
    source_season = next(
        row
        for row in value["source_seasons"]
        if row["season"] == first["season"] and row["source"] == first["source"]
    )
    source_season["split_receipt_sha256s"] = ["d" * 64, "e" * 64]
    value["split_receipt_sha256s"] = ["d" * 64, "e" * 64]
    value["counts"]["split_receipt_count"] = 2
    return _canonical_raw(value)
