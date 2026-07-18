from __future__ import annotations

import builtins
import os
import re
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request
import json
from dataclasses import FrozenInstanceError

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
