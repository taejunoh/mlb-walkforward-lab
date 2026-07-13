from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch
import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest

import mlb_backtest.protocols as protocols
from mlb_backtest.diagnostics import canonical_json_bytes, deep_freeze, sha256_file
from mlb_backtest.protocols import (
    CHANGE_CHANGED,
    CHANGE_FORBIDDEN,
    CHANGE_UNCHANGED,
    INTERNAL_METRIC_LITERALS,
    FOLD_REQUIRED_KEYS,
    FOLD_LITERALS,
    HISTORICAL_DATE_QUEUE,
    HISTORICAL_DENOMINATORS,
    HISTORICAL_OUTCOME_ISOLATION,
    HISTORICAL_SELECTION,
    HISTORICAL_TERMINAL,
    RUN_REQUIRED_KEYS,
    PROSPECTIVE_REQUIRED_KEYS,
    V52_REQUIRED_KEYS,
    classify_uncertainty,
    classify_snapshot,
    combine_ledgers,
    classify_v52,
    data_completeness_score,
    date_disposition,
    direction_from_p_home,
    eligibility_disposition,
    eligible_for_prospective_official_pool,
    expected_v52_config,
    expected_historical_run,
    expected_prospective_spec,
    load_run_spec,
    load_prospective_spec,
    load_change_contract,
    load_v52_config,
    optional_neutral,
    parse_change_contract,
    parse_run_spec,
    parse_prospective_spec,
    parse_v52_config,
    ranking_key,
    read_legacy_confidence,
    validate_fold,
    validate_run,
    write_historical_contract,
    write_prospective_contract,
    write_v52_config_contracts,
)


ROOT = Path(__file__).resolve().parents[1]
V52_CONFIG = ROOT / "config/v5_2_qd.json"
V52_SCHEMA = ROOT / "schemas/v5_2_qd.schema.json"
CHANGE_CONTRACT = ROOT / "protocols/v5_2_qd/change_contract.json"
HISTORICAL_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
RUN_SCHEMA = ROOT / "schemas/v5_2_run_spec.schema.json"
PROSPECTIVE_SPEC = ROOT / "protocols/v5_2_qd/prospective_2026.json"
PROSPECTIVE_SCHEMA = ROOT / "schemas/v5_2_prospective.schema.json"


class ProspectiveProtocolTest(unittest.TestCase):
    def _trusted_clock(self, value):
        return patch(
            "mlb_backtest.protocols._trusted_utc_now",
            return_value=datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ"),
        )

    def _sealed_snapshot_ledger(
        self,
        root,
        *,
        game_pk=777001,
        official_date="2026-07-13",
        basis="2026-07-13T23:10:00Z",
        receipts=(
            ("2026-07-13T22:24:00Z", True, True),
        ),
    ):
        with self._trusted_clock("2026-07-13T21:00:00Z"):
            protocols.initialize_prospective_snapshot_ledger(
                root,
                official_date=official_date,
                game_pk=game_pk,
                lock_basis_start_utc=basis,
            )
        for receipt_at, successful, complete in receipts:
            with self._trusted_clock(receipt_at):
                protocols.record_prospective_snapshot(
                    root,
                    official_date=official_date,
                    game_pk=game_pk,
                    observed_scheduled_start_utc=basis,
                    successful=successful,
                    away_starter_present=complete,
                    home_starter_present=complete,
                    away_lineup_count=9 if complete else 8,
                    home_lineup_count=9,
                    ntp_offset_seconds=0.25,
                )
        with self._trusted_clock("2026-07-13T22:25:00Z"):
            return protocols.seal_prospective_snapshot_ledger(
                root,
                official_date=official_date,
                game_pk=game_pk,
            )

    def _disk_candidate(self, root, *, game_pk=777001, p_home=0.63):
        return protocols.build_prospective_candidate(
            root,
            official_date="2026-07-13",
            game_pk=game_pk,
            p_home=p_home,
            critical=False,
            eligibility_status="ELIGIBLE",
            data_completeness_score=8.5,
            absolute_starter_edge=4.0,
        )

    def test_snapshot_ledger_uses_trusted_clock_and_seals_disk_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = self._sealed_snapshot_ledger(
                root,
                receipts=(
                    ("2026-07-13T22:20:00Z", True, True),
                    ("2026-07-13T22:24:00Z", True, True),
                    ("2026-07-13T22:24:30Z", False, False),
                ),
            )
            loaded, receipts = protocols.load_prospective_snapshot_ledger(
                root,
                official_date="2026-07-13",
                game_pk=777001,
            )
            self.assertEqual(loaded, state)
            self.assertEqual(state.status, "CLOSED")
            self.assertEqual(state.snapshot_count, 3)
            self.assertEqual(state.selected_sequence, 2)
            self.assertEqual(receipts[-1].snapshot.receipt_at_utc, "2026-07-13T22:24:30Z")
            self.assertEqual(receipts[-1].persisted_at_utc, "2026-07-13T22:24:30Z")
            self.assertEqual(receipts[-1].snapshot.sequence, 3)
            self.assertEqual(receipts[-1].snapshot.complete_participants, False)
            self.assertEqual(receipts[-1].prior_receipt_hash, receipts[-2].receipt_hash)
            self.assertRegex(state.head_hash, r"^[0-9a-f]{64}$")
            self.assertIn("snapshot_ledgers/2026-07-13/777001", state.audit_path)

    def test_snapshot_ledger_rejects_backfill_late_append_and_empty_root_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2020-07-13T21:00:00Z"):
                with self.assertRaisesRegex(ValueError, "first permitted|backfill"):
                    protocols.initialize_prospective_snapshot_ledger(
                        root,
                        official_date="2020-07-13",
                        game_pk=777001,
                        lock_basis_start_utc="2020-07-13T23:10:00Z",
                    )
            with self.assertRaisesRegex(ValueError, "ledger.*missing|SYSTEM_MISS"):
                protocols.build_prospective_candidate(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    p_home=0.63,
                    critical=False,
                    eligibility_status="ELIGIBLE",
                    data_completeness_score=8.5,
                    absolute_starter_edge=4.0,
                )
            self._sealed_snapshot_ledger(root)
            with self._trusted_clock("2026-07-13T22:25:01Z"):
                with self.assertRaisesRegex(ValueError, "closed|after lock"):
                    protocols.record_prospective_snapshot(
                        root,
                        official_date="2026-07-13",
                        game_pk=777001,
                        observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                        successful=False,
                        away_starter_present=False,
                        home_starter_present=False,
                        away_lineup_count=0,
                        home_lineup_count=0,
                        ntp_offset_seconds=0.25,
                    )

    def test_candidate_digest_binds_full_ordered_ledger_history(self):
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first_root = Path(first_temp).resolve()
            second_root = Path(second_temp).resolve()
            self._sealed_snapshot_ledger(first_root)
            self._sealed_snapshot_ledger(
                second_root,
                receipts=(
                    ("2026-07-13T22:24:00Z", True, True),
                    ("2026-07-13T22:24:30Z", False, False),
                ),
            )
            first = self._disk_candidate(first_root)
            second = self._disk_candidate(second_root)
            self.assertNotEqual(
                protocols._prospective_candidate_raw(first),
                protocols._prospective_candidate_raw(second),
            )
            self.assertEqual(len(second.snapshot_history), 2)
            self.assertEqual(second.snapshot_ledger_count, 2)
            self.assertEqual(second.selected_snapshot.sequence, 1)
            self.assertEqual(second.snapshot_ledger_state["status"], "CLOSED")
            self.assertEqual(second.snapshot_ledger_state["snapshot_count"], 2)
            self.assertNotEqual(first.snapshot_ledger_digest, second.snapshot_ledger_digest)

    def test_official_slate_reloads_sealed_ledgers_and_rejects_fabrication_or_rollback(self):
        fabricated = self._candidate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                with self.assertRaisesRegex(ValueError, "ledger.*missing|SYSTEM_MISS"):
                    protocols.write_prospective_candidate_slate(
                        root,
                        official_date="2026-07-13",
                        candidates=(fabricated,),
                    )
            self._sealed_snapshot_ledger(
                root,
                receipts=(
                    ("2026-07-13T22:24:00Z", True, True),
                    ("2026-07-13T22:24:30Z", False, False),
                ),
            )
            candidate = self._disk_candidate(root)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(slate.expected_candidate_count, 1)
            state, receipts = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(state.snapshot_count, 2)
            (root / receipts[-1].audit_path).unlink()
            with self.assertRaisesRegex(ValueError, "tail rollback|count/head"):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )

    def test_snapshot_ledger_parallel_append_is_serialized_and_tail_rollback_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )

            def append(successful):
                with self._trusted_clock("2026-07-13T22:24:00Z"):
                    return protocols.record_prospective_snapshot(
                        root,
                        official_date="2026-07-13",
                        game_pk=777001,
                        observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                        successful=successful,
                        away_starter_present=True,
                        home_starter_present=True,
                        away_lineup_count=9,
                        home_lineup_count=9,
                        ntp_offset_seconds=0.25,
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                receipts = tuple(executor.map(append, (True, False)))
            self.assertEqual({receipt.sequence for receipt in receipts}, {1, 2})
            state, loaded = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(state.snapshot_count, 2)
            self.assertEqual(tuple(receipt.sequence for receipt in loaded), (1, 2))
            state_path = root / state.audit_path
            raw = json.loads(state_path.read_bytes())
            raw["snapshot_count"] = 1
            state_path.write_bytes(canonical_json_bytes(raw) + b"\n")
            with self.assertRaisesRegex(ValueError, "tail rollback|count/head"):
                protocols.load_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )

    def test_snapshot_ledger_rejects_symlink_and_hardlink_paths(self):
        for alias_type in ("ancestor_symlink", "lock_hardlink"):
            with self.subTest(alias_type=alias_type), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                lock_parent = (
                    root
                    / "audit/v5_2_qd/prospective_2026/snapshot_locks/2026-07-13"
                )
                if alias_type == "ancestor_symlink":
                    lock_parent.parent.mkdir(parents=True)
                    outside = root / "outside"
                    outside.mkdir()
                    lock_parent.symlink_to(outside, target_is_directory=True)
                else:
                    lock_parent.mkdir(parents=True)
                    target = root / "hardlink-target"
                    target.write_bytes(b"")
                    os.link(target, lock_parent / "777001.lock")
                with self._trusted_clock("2026-07-13T21:00:00Z"):
                    with self.assertRaisesRegex(ValueError, "symlink|hardlink|alias"):
                        protocols.initialize_prospective_snapshot_ledger(
                            root,
                            official_date="2026-07-13",
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                        )

    def test_snapshot_ledger_does_not_create_repository_observations(self):
        observed_root = ROOT / "data/v5_2_qd/prospective_2026"
        before = tuple(observed_root.rglob("*")) if observed_root.exists() else ()
        with tempfile.TemporaryDirectory() as temporary:
            self._sealed_snapshot_ledger(Path(temporary).resolve())
        after = tuple(observed_root.rglob("*")) if observed_root.exists() else ()
        self.assertEqual(after, before)

    def test_snapshot_append_postcondition_failure_rolls_back_receipt_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            real_load = protocols.load_prospective_snapshot_ledger
            calls = 0

            def fail_second_load(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError("snapshot append postcondition failed")
                return real_load(*args, **kwargs)

            with (
                patch(
                    "mlb_backtest.protocols.load_prospective_snapshot_ledger",
                    side_effect=fail_second_load,
                ),
                self._trusted_clock("2026-07-13T22:24:00Z"),
                self.assertRaisesRegex(ValueError, "postcondition"),
            ):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    ntp_offset_seconds=0.25,
                )
            receipt_dir = (
                root
                / "audit/v5_2_qd/prospective_2026/snapshot_receipts/"
                "2026-07-13/777001"
            )
            self.assertFalse(receipt_dir.exists())
            state, receipts = real_load(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(state.snapshot_count, 0)
            self.assertEqual(receipts, ())
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                receipt = protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    ntp_offset_seconds=0.25,
                )
            self.assertEqual(receipt.sequence, 1)

    def test_snapshot_receipt_partial_write_never_deletes_replaced_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot = self._snapshot(sequence=1, receipt="2026-07-13T22:24:00Z")
            snapshot_sha = hashlib.sha256(
                canonical_json_bytes(protocols._prospective_snapshot_raw(snapshot))
            ).hexdigest()
            unsigned = protocols.ProspectiveSnapshotReceipt(
                official_date="2026-07-13",
                game_pk=777001,
                sequence=1,
                prior_receipt_hash="0" * 64,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha,
                persisted_at_utc="2026-07-13T22:24:00Z",
                receipt_hash="",
                audit_path="",
            )
            receipt_hash = protocols._snapshot_receipt_hash(unsigned)
            relative = protocols._snapshot_receipt_relative_path(
                "2026-07-13", 777001, 1, receipt_hash
            )
            receipt = replace(
                unsigned,
                receipt_hash=receipt_hash,
                audit_path=relative.as_posix(),
            )
            receipt_path = root / relative
            original_write = os.write

            def replace_then_fail(descriptor, contents):
                original_write(descriptor, bytes(contents[:1]))
                receipt_path.unlink()
                receipt_path.write_bytes(b"external replacement")
                raise OSError("simulated partial write")

            with (
                patch("mlb_backtest.protocols.os.write", side_effect=replace_then_fail),
                self.assertRaisesRegex(Exception, "replaced|recovery|partial"),
            ):
                protocols._write_snapshot_receipt_exclusive(root, receipt)
            self.assertEqual(receipt_path.read_bytes(), b"external replacement")

    def test_snapshot_state_anchor_detects_reopen_and_coordinated_tail_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            state, receipts = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            state_path = root / state.audit_path
            raw = json.loads(state_path.read_bytes())
            raw.update(
                {
                    "status": "OPEN",
                    "selected_sequence": None,
                    "selected_snapshot_id": None,
                    "selected_snapshot_sha256": None,
                    "closed_at_utc": None,
                }
            )
            state_path.write_bytes(canonical_json_bytes(raw) + b"\n")
            with self.assertRaisesRegex(ValueError, "anchor|state digest|revision"):
                protocols.load_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            append_kwargs = {
                "official_date": "2026-07-13",
                "game_pk": 777001,
                "observed_scheduled_start_utc": "2026-07-13T23:10:00Z",
                "successful": True,
                "away_starter_present": True,
                "home_starter_present": True,
                "away_lineup_count": 9,
                "home_lineup_count": 9,
                "ntp_offset_seconds": 0.25,
            }
            with self._trusted_clock("2026-07-13T22:23:00Z"):
                protocols.record_prospective_snapshot(root, **append_kwargs)
            first_state, _ = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            first_state_bytes = (root / first_state.audit_path).read_bytes()
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                protocols.record_prospective_snapshot(root, **append_kwargs)
            _, current_receipts = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            (root / current_receipts[-1].audit_path).unlink()
            (root / first_state.audit_path).write_bytes(first_state_bytes)
            with self.assertRaisesRegex(ValueError, "anchor|rollback|revision"):
                protocols.load_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )

    def test_snapshot_close_time_and_clock_sampling_are_inside_lock(self):
        with patch(
            "mlb_backtest.protocols._trusted_utc_now",
            return_value=datetime(2026, 7, 13, 22, 25, 0, 1),
        ):
            self.assertEqual(
                protocols._trusted_utc_now_string(), "2026-07-13T22:25:01Z"
            )
        with tempfile.TemporaryDirectory() as temporary:
            early_root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    early_root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with (
                patch(
                    "mlb_backtest.protocols._trusted_utc_now",
                    return_value=datetime(2026, 7, 13, 22, 24, 59, 1),
                ),
                self.assertRaisesRegex(ValueError, "before lock"),
            ):
                protocols.seal_prospective_snapshot_ledger(
                    early_root,
                    official_date="2026-07-13",
                    game_pk=777001,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            entered = False
            real_lock = protocols._snapshot_ledger_lock

            @protocols.contextmanager
            def delayed_lock(*args, **kwargs):
                nonlocal entered
                with real_lock(*args, **kwargs) as locked_root:
                    entered = True
                    yield locked_root

            def boundary_clock():
                value = (
                    "2026-07-13T22:25:01Z"
                    if entered
                    else "2026-07-13T22:24:59Z"
                )
                return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")

            with (
                patch(
                    "mlb_backtest.protocols._snapshot_ledger_lock",
                    side_effect=delayed_lock,
                ),
                patch(
                    "mlb_backtest.protocols._trusted_utc_now",
                    side_effect=boundary_clock,
                ),
                self.assertRaisesRegex(ValueError, "after lock"),
            ):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    ntp_offset_seconds=0.25,
                )

            loaded = False
            real_load = protocols.load_prospective_snapshot_ledger

            def delayed_load(*args, **kwargs):
                nonlocal loaded
                result = real_load(*args, **kwargs)
                loaded = True
                return result

            def post_load_clock():
                value = (
                    "2026-07-13T22:25:01Z"
                    if loaded
                    else "2026-07-13T22:24:59Z"
                )
                return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")

            with (
                patch(
                    "mlb_backtest.protocols.load_prospective_snapshot_ledger",
                    side_effect=delayed_load,
                ),
                patch(
                    "mlb_backtest.protocols._trusted_utc_now",
                    side_effect=post_load_clock,
                ),
                self.assertRaisesRegex(ValueError, "after lock"),
            ):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    ntp_offset_seconds=0.25,
                )

            state_path = (
                root
                / "audit/v5_2_qd/prospective_2026/snapshot_ledgers/"
                "2026-07-13/777001/state.json"
            )
            raw = json.loads(state_path.read_bytes())
            raw.update(
                {
                    "status": "CLOSED",
                    "closed_at_utc": "2020-01-01T00:00:00Z",
                }
            )
            state_path.write_bytes(canonical_json_bytes(raw) + b"\n")
            with self.assertRaisesRegex(ValueError, "close|chronology|anchor"):
                protocols.load_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )

    def test_current_approved_design_pin_matches_reviewed_blob(self):
        design_path = (
            ROOT
            / "docs/superpowers/specs/"
            "2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
        )
        self.assertEqual(
            getattr(protocols, "CURRENT_APPROVED_DESIGN_COMMIT", None),
            "fb728b3",
        )
        self.assertEqual(
            getattr(protocols, "CURRENT_APPROVED_DESIGN_SHA256", None),
            "551e9f83e8f10ba7d7db3ee2cc756d4e9493eba756dd118babe74b95b513330c",
        )
        self.assertEqual(protocols.approved_design_bytes(ROOT), design_path.read_bytes())
        self.assertEqual(
            hashlib.sha256(protocols.approved_design_bytes(ROOT)).hexdigest(),
            protocols.CURRENT_APPROVED_DESIGN_SHA256,
        )

    def _snapshot(
        self,
        *,
        game_pk=777001,
        sequence=1,
        basis="2026-07-13T23:10:00Z",
        scheduled="2026-07-13T23:10:00Z",
        receipt="2026-07-13T22:22:00Z",
        lock="2026-07-13T22:25:00Z",
        successful=True,
        complete=True,
        offset=0.25,
    ):
        return protocols.ProspectiveSnapshot(
            game_pk=game_pk,
            lock_basis_start_utc=basis,
            observed_scheduled_start_utc=scheduled,
            lock_at_utc=lock,
            receipt_at_utc=receipt,
            successful=successful,
            complete_participants=complete,
            ntp_offset_seconds=offset,
            sequence=sequence,
        )

    def _candidate(
        self,
        *,
        game_pk=777001,
        official_date="2026-07-13",
        recorded_at="2026-07-13T22:25:00Z",
        retrospective_backfill=False,
        p_home=0.63,
        predicted_side="home",
        snapshot_id="snapshot-777001-4",
        snapshot_hash="a" * 64,
        sealed_snapshot_id="snapshot-777001-4",
        sealed_snapshot_hash="a" * 64,
        critical=False,
        eligibility_status="ELIGIBLE",
        date_state="CANDIDATE_ACTIVE",
        lock_receipt="2026-07-13T22:25:00Z",
        completeness=8.5,
        starter_edge=4.0,
        snapshot_sequence=4,
        snapshot_candidates=None,
        lock_basis="2026-07-13T23:10:00Z",
    ):
        del snapshot_id, snapshot_hash, sealed_snapshot_id, sealed_snapshot_hash
        verified_snapshot = self._snapshot(
            game_pk=game_pk,
            sequence=snapshot_sequence,
            receipt=recorded_at,
            lock=lock_receipt,
            basis=lock_basis,
            scheduled=lock_basis,
        )
        snapshot_raw = {
            "gamePk": verified_snapshot.game_pk,
            "lock_basis_start_utc": verified_snapshot.lock_basis_start_utc,
            "observed_scheduled_start_utc": (
                verified_snapshot.observed_scheduled_start_utc
            ),
            "lock_at_utc": verified_snapshot.lock_at_utc,
            "receipt_at_utc": verified_snapshot.receipt_at_utc,
            "successful": verified_snapshot.successful,
            "complete_participants": verified_snapshot.complete_participants,
            "ntp_offset_seconds": verified_snapshot.ntp_offset_seconds,
            "sequence": verified_snapshot.sequence,
        }
        verified_snapshot_sha256 = hashlib.sha256(
            canonical_json_bytes(snapshot_raw)
        ).hexdigest()
        verified_snapshot_id = (
            f"snapshot:{game_pk}:{snapshot_sequence}:{verified_snapshot_sha256}"
        )
        return protocols.ProspectiveCandidateRow(
            game_pk=game_pk,
            official_date=official_date,
            recorded_at_utc=recorded_at,
            retrospective_backfill=retrospective_backfill,
            p_home=p_home,
            predicted_side=predicted_side,
            verified_snapshot_id=verified_snapshot_id,
            verified_snapshot_sha256=verified_snapshot_sha256,
            sealed_lock_snapshot_id=verified_snapshot_id,
            sealed_lock_snapshot_sha256=verified_snapshot_sha256,
            provenance="TIMESTAMP_VERIFIED_T45",
            lock_state="SEALED",
            final_snapshot_classification="TIMESTAMP_VERIFIED_T45",
            critical=critical,
            eligibility_status=eligibility_status,
            date_state=date_state,
            data_completeness_score=completeness,
            absolute_starter_edge=starter_edge,
            lock_basis_start_utc=lock_basis,
            lock_receipt_at_utc=lock_receipt,
            verified_snapshot=verified_snapshot,
            snapshot_candidates=(
                tuple(snapshot_candidates)
                if snapshot_candidates is not None
                else (verified_snapshot,)
            ),
        )

    def _empty_chain_state(self):
        return protocols.PostlockChainState(
            game_pk=777001,
            lock_basis_start_utc="2026-07-13T23:10:00Z",
            event_count=0,
            head_hash="0" * 64,
            persisted_at_utc="2026-07-13T22:25:00Z",
            audit_path=(
                "audit/v5_2_qd/prospective_2026/"
                "postlock_chain_state/777001.json"
            ),
            event_log_path=(
                "audit/v5_2_qd/prospective_2026/"
                "postlock_start_changes/777001.jsonl"
            ),
        )

    def test_exact_2026_oof_and_timestamp_contract(self):
        spec = load_prospective_spec(PROSPECTIVE_SPEC)
        self.assertEqual(spec.first_permitted_date.isoformat(), "2026-07-13")
        self.assertEqual(spec.fit_years, tuple(range(2018, 2025)))
        self.assertNotIn(2025, spec.fit_years)
        self.assertEqual(spec.oof_targets, (2019, 2020, 2021, 2022, 2023, 2024))
        self.assertEqual(
            spec.polling,
            deep_freeze(
                {
                    "T-90_to_before_T-50_seconds": 300,
                    "T-50_to_T-45_seconds": 60,
                    "qualifying_window_minutes": [-50, -45],
                    "max_age_at_lock_minutes": 5,
                    "max_ntp_offset_seconds": 2,
                }
            ),
        )

    def test_historical_rows_misses_and_partial_snapshots_cannot_enter_pool(self):
        for stratum in (
            "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "TIMESTAMP_PARTIAL_T45",
        ):
            self.assertFalse(
                eligible_for_prospective_official_pool(
                    replace(self._candidate(), provenance=stratum)
                )
            )
        with self.assertRaisesRegex(ValueError, "pooling forbidden"):
            combine_ledgers("historical_walkforward_500", "prospective_2026")
        self.assertEqual(date_disposition(["acquisition_failure"]), "SYSTEM_MISS")

    def test_unknown_missing_nested_and_tampered_values_fail_closed(self):
        raw = json.loads(PROSPECTIVE_SPEC.read_text())
        for mutate in (
            lambda value: value.__setitem__("unknown", 1),
            lambda value: value.pop("polling"),
            lambda value: value["polling"].pop("max_ntp_offset_seconds"),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, "keys"):
                parse_prospective_spec(changed)
        changed = copy.deepcopy(raw)
        changed["schedule_changes"]["relock_forbidden"] = False
        with self.assertRaisesRegex(ValueError, "prospective spec differs"):
            parse_prospective_spec(changed)

    def test_clock_pool_and_postlock_immutability_literal_contract(self):
        spec = load_prospective_spec(PROSPECTIVE_SPEC)
        self.assertEqual(spec.raw, deep_freeze(expected_prospective_spec()))
        self.assertEqual(
            classify_snapshot(self._snapshot()), "TIMESTAMP_VERIFIED_T45"
        )
        self.assertEqual(
            classify_snapshot(self._snapshot(complete=False)),
            "TIMESTAMP_PARTIAL_T45",
        )
        self.assertEqual(date_disposition(["run_invalid"]), "RUN_INVALID")
        self.assertEqual(
            date_disposition(["partial_only"]),
            "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE",
        )
        self.assertEqual(
            date_disposition(["empty_verified"]), "NO_VERIFIED_ELIGIBLE_DATE"
        )
        changes = spec.raw["schedule_changes"]
        self.assertEqual(changes["postlock_mutable_field"], "latest_effective_start_utc")
        self.assertIn("lock_basis_start_utc", changes["immutable_fields"])
        self.assertIn("top1", changes["immutable_fields"])
        self.assertTrue(changes["relock_forbidden"])
        self.assertTrue(spec.raw["backfill_forbidden"])
        self.assertTrue(spec.raw["pooling_forbidden"])
        ledger = spec.raw["clock"]["snapshot_ledger"]
        self.assertEqual(ledger["receipt_time_source"], "internal_trusted_clock_only")
        self.assertEqual(ledger["append_receipt_write"], "O_EXCL_fsync_read_only")
        self.assertEqual(ledger["close_policy"], "at_lock_from_disk_history")
        self.assertTrue(ledger["append_after_close_forbidden"])
        self.assertIn("{official_date}/{gamePk}", ledger["state_path_template"])
        self.assertIn("snapshot_state_anchors", ledger["state_anchor_path_template"])
        self.assertEqual(
            ledger["state_anchor_write"], "O_EXCL_fsync_read_only_every_transition"
        )
        candidate_fields = spec.raw["official_pool"]["candidate_record_fields"]
        for field in (
            "snapshot_history",
            "selected_snapshot",
            "snapshot_ledger_state",
            "snapshot_ledger_head",
            "snapshot_ledger_count",
            "snapshot_ledger_path",
            "snapshot_ledger_digest",
        ):
            self.assertIn(field, candidate_fields)

    def test_snapshot_record_validates_clock_basis_window_and_latest_selection(self):
        early = self._snapshot(sequence=1, receipt="2026-07-13T22:20:00Z")
        later = self._snapshot(sequence=2, receipt="2026-07-13T22:24:59Z")
        partial = self._snapshot(
            sequence=3,
            receipt="2026-07-13T22:25:00Z",
            complete=False,
        )
        failed = self._snapshot(
            sequence=4,
            receipt="2026-07-13T22:25:00Z",
            successful=False,
        )
        self.assertEqual(classify_snapshot(early), "TIMESTAMP_VERIFIED_T45")
        self.assertEqual(classify_snapshot(later), "TIMESTAMP_VERIFIED_T45")
        self.assertEqual(classify_snapshot(partial), "TIMESTAMP_PARTIAL_T45")
        self.assertEqual(classify_snapshot(failed), "UNSUCCESSFUL_RESPONSE_REJECTED")
        self.assertEqual(
            protocols.select_qualifying_snapshot((early, later, partial, failed)),
            later,
        )

    def test_snapshot_record_rejects_basis_lock_receipt_and_sequence_mismatches(self):
        cases = (
            (self._snapshot(scheduled="2026-07-13T23:11:00Z"), "scheduled start"),
            (self._snapshot(lock="2026-07-13T22:24:59Z"), "basis minus 45"),
            (self._snapshot(receipt="2026-07-13T22:25:01Z"), "after lock"),
            (self._snapshot(receipt="2026-07-13T22:19:59Z"), "qualifying window"),
            (self._snapshot(offset=2.000001), "SYSTEM_MISS_CLOCK"),
            (self._snapshot(sequence=0), "sequence"),
        )
        for snapshot, message in cases:
            with self.subTest(message=message):
                if message == "SYSTEM_MISS_CLOCK":
                    self.assertEqual(classify_snapshot(snapshot), message)
                else:
                    with self.assertRaisesRegex(ValueError, message):
                        classify_snapshot(snapshot)
        with self.assertRaisesRegex(ValueError, "canonical UTC"):
            classify_snapshot(
                self._snapshot(receipt="2026-07-13T22:22:00+00:00")
            )
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            protocols.select_qualifying_snapshot(
                (self._snapshot(sequence=2), self._snapshot(sequence=1))
            )
        for snapshot in (
            replace(self._snapshot(), game_pk=True),
            replace(self._snapshot(), sequence=True),
        ):
            with self.assertRaisesRegex(ValueError, "positive integers"):
                classify_snapshot(snapshot)

    def test_snapshot_record_rejects_pre_protocol_backfill(self):
        for basis, lock_at, receipt in (
            (
                "2020-07-13T23:10:00Z",
                "2020-07-13T22:25:00Z",
                "2020-07-13T22:24:00Z",
            ),
            (
                "2026-07-13T00:10:00Z",
                "2026-07-12T23:25:00Z",
                "2026-07-12T23:24:00Z",
            ),
        ):
            with self.subTest(basis=basis):
                historical = protocols.ProspectiveSnapshot(
                    game_pk=777001,
                    lock_basis_start_utc=basis,
                    observed_scheduled_start_utc=basis,
                    lock_at_utc=lock_at,
                    receipt_at_utc=receipt,
                    successful=True,
                    complete_participants=True,
                    ntp_offset_seconds=0.25,
                    sequence=1,
                )
                with self.assertRaisesRegex(ValueError, "first permitted|backfill"):
                    classify_snapshot(historical)

    def test_official_pool_full_row_gate_and_date_precedence(self):
        eligible = self._candidate()
        self.assertTrue(eligible_for_prospective_official_pool(eligible))
        mutations = (
            replace(eligible, lock_state="UNSEALED"),
            replace(eligible, final_snapshot_classification="TIMESTAMP_PARTIAL_T45"),
            replace(eligible, critical=True),
            replace(eligible, eligibility_status="EXCLUDE"),
            replace(eligible, date_state="SYSTEM_MISS"),
        )
        for changed in mutations:
            with self.subTest(changed=changed):
                self.assertFalse(eligible_for_prospective_official_pool(changed))

        precedence_cases = (
            ({"lock_top1"}, "LOCK_TOP1"),
            ({"empty_verified", "lock_top1"}, "NO_VERIFIED_ELIGIBLE_DATE"),
            (
                {"partial_only", "empty_verified", "lock_top1"},
                "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE",
            ),
            (
                {"acquisition_failure", "partial_only", "lock_top1"},
                "SYSTEM_MISS",
            ),
            (
                {"run_invalid", "acquisition_failure", "partial_only"},
                "RUN_INVALID",
            ),
            ({"clock_failure", "partial_only"}, "SYSTEM_MISS"),
        )
        for active, expected in precedence_cases:
            with self.subTest(active=active):
                self.assertEqual(date_disposition(active), expected)

    def test_candidate_gate_rejects_missing_date_backfill_half_and_unbound_snapshot(self):
        invalid = (
            (replace(self._candidate(), official_date=""), "official_date"),
            (
                replace(self._candidate(), official_date="2026-07-12"),
                "first permitted",
            ),
            (
                replace(self._candidate(), retrospective_backfill=True),
                "backfill",
            ),
            (replace(self._candidate(), p_home=0.5), "p_home"),
            (
                replace(self._candidate(), predicted_side="away"),
                "predicted side",
            ),
            (
                replace(self._candidate(), sealed_lock_snapshot_id="other"),
                "snapshot binding",
            ),
            (
                replace(self._candidate(), sealed_lock_snapshot_sha256="b" * 64),
                "snapshot binding",
            ),
            (
                replace(self._candidate(), verified_snapshot_sha256=None),
                "snapshot binding",
            ),
            (
                replace(self._candidate(), recorded_at_utc="2026-07-13T22:25:01Z"),
                "retrospective",
            ),
            (replace(self._candidate(), date_state="LOCK_TOP1"), "preclaim"),
        )
        for candidate, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    eligible_for_prospective_official_pool(candidate)

    def test_candidate_gate_rejects_caller_asserted_historical_receipt(self):
        candidate = replace(
            self._candidate(),
            recorded_at_utc="2020-01-01T00:00:00Z",
            lock_receipt_at_utc="2020-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "snapshot|receipt|backfill"):
            eligible_for_prospective_official_pool(candidate)

    def test_candidate_record_carries_verified_snapshot_and_selection_history(self):
        fields = protocols.ProspectiveCandidateRow.__dataclass_fields__
        self.assertIn("verified_snapshot", fields)
        self.assertIn("snapshot_candidates", fields)

    def test_candidate_gate_binds_canonical_selected_snapshot_and_exact_receipt(self):
        candidate = self._candidate(recorded_at="2026-07-13T22:24:00Z")
        self.assertTrue(eligible_for_prospective_official_pool(candidate))
        later = self._snapshot(
            sequence=5,
            receipt="2026-07-13T22:24:59Z",
        )
        invalid = (
            (
                replace(candidate, verified_snapshot=later),
                "canonical|snapshot",
            ),
            (
                replace(
                    candidate,
                    snapshot_candidates=(*candidate.snapshot_candidates, later),
                ),
                "latest|selected",
            ),
            (
                replace(candidate, recorded_at_utc="2026-07-13T22:23:59Z"),
                "recorded_at|receipt",
            ),
            (
                replace(candidate, official_date="2026-07-14"),
                "official_date|basis",
            ),
        )
        for changed, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    eligible_for_prospective_official_pool(changed)

    def test_slate_manifest_persists_full_selected_snapshot_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            candidate = self._disk_candidate(root)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            receipt = dict(slate.snapshot_lock_receipts[0])
            self.assertEqual(
                set(receipt),
                {
                    "candidate_id",
                    "verified_snapshot_id",
                    "verified_snapshot_sha256",
                    "lock_basis_start_utc",
                    "lock_at_utc",
                    "receipt_at_utc",
                },
            )
            self.assertEqual(
                receipt["verified_snapshot_sha256"],
                candidate.verified_snapshot_sha256,
            )
            self.assertEqual(
                receipt["lock_at_utc"], candidate.verified_snapshot.lock_at_utc
            )
            self.assertEqual(
                receipt["receipt_at_utc"], candidate.verified_snapshot.receipt_at_utc
            )

    def test_final_selection_waits_for_all_locks_and_applies_exact_tie_break(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            self._sealed_snapshot_ledger(root, game_pk=777002)
            first = self._disk_candidate(root, game_pk=777002)
            second = self._disk_candidate(root, game_pk=777001)
            with self.assertRaisesRegex(ValueError, "slate.*missing|missing.*slate"):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(first, second),
                )
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(first, second),
                )
            self.assertTrue(slate.all_locks_sealed)
            self.assertEqual(slate.final_lock_receipt_at_utc, "2026-07-13T22:25:00Z")
            top1 = protocols.select_prospective_top1(
                root,
                official_date="2026-07-13",
                candidates=(first, second),
            )
            self.assertEqual(top1.game_pk, 777001)
            self.assertEqual(top1.official_rank, 1)
            self.assertEqual(top1.official_date, "2026-07-13")
            self.assertEqual(
                top1.verified_snapshot_sha256,
                second.verified_snapshot_sha256,
            )

    def test_final_selection_rejects_mixed_dates_empty_pool_and_early_final_receipt(self):
        candidate = self._candidate()
        other_date = self._candidate(
            game_pk=777002,
            official_date="2026-07-14",
            recorded_at="2026-07-14T22:25:00Z",
            lock_receipt="2026-07-14T22:25:00Z",
            lock_basis="2026-07-14T23:10:00Z",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            for candidates, message in (
                ((candidate, other_date), "one official date"),
                ((), "at least one eligible"),
                ((replace(candidate, critical=True),), "eligible"),
            ):
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        with self._trusted_clock("2026-07-13T22:26:00Z"):
                            protocols.write_prospective_candidate_slate(
                                root,
                                official_date="2026-07-13",
                                candidates=candidates,
                            )

    def test_slate_binding_rejects_candidate_omission_addition_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for game_pk in (777001, 777002, 777003):
                self._sealed_snapshot_ledger(root, game_pk=game_pk)
            first = self._disk_candidate(root, game_pk=777001)
            second = self._disk_candidate(root, game_pk=777002)
            third = self._disk_candidate(root, game_pk=777003)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(first, second),
                )
            for candidates, message in (
                ((first,), "count|IDs"),
                ((first, second, third), "count|IDs"),
                ((replace(first, p_home=0.64), second), "digest"),
            ):
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        protocols.select_prospective_top1(
                            root,
                            official_date="2026-07-13",
                            candidates=candidates,
                        )
            slate_path = root / slate.audit_path
            raw = json.loads(slate_path.read_text())
            raw["expected_candidate_count"] = 1
            slate_path.write_bytes(canonical_json_bytes(raw) + b"\n")
            with self.assertRaisesRegex(ValueError, "manifest|count"):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(first, second),
                )

    def test_slate_writer_is_transactional_canonical_and_mode_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            candidate = self._disk_candidate(root)
            with patch(
                "mlb_backtest.protocols.load_prospective_candidate_slate",
                side_effect=ValueError("slate postcondition failed"),
            ):
                with self.assertRaisesRegex(ValueError, "postcondition"):
                    with self._trusted_clock("2026-07-13T22:26:00Z"):
                        protocols.write_prospective_candidate_slate(
                            root,
                            official_date="2026-07-13",
                            candidates=(candidate,),
                        )
            slate_path = (
                root
                / "audit/v5_2_qd/prospective_2026/candidate_slates/2026-07-13.json"
            )
            slate_lock_path = (
                root
                / "audit/v5_2_qd/prospective_2026/"
                "candidate_slate_locks/2026-07-13.lock"
            )
            self.assertFalse(slate_path.exists())
            self.assertTrue(slate_lock_path.exists())
            self.assertEqual(stat.S_IMODE(slate_lock_path.stat().st_mode), 0o600)
            self.assertEqual(slate_lock_path.stat().st_nlink, 1)
            self.assertEqual(list(root.rglob(".*.tmp")), [])
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(stat.S_IMODE(slate_path.stat().st_mode), 0o644)
            self.assertEqual(
                slate_path.read_bytes(),
                canonical_json_bytes(json.loads(slate_path.read_bytes())) + b"\n",
            )
            self.assertEqual(
                protocols.load_prospective_candidate_slate(
                    root, official_date="2026-07-13"
                ),
                slate,
            )

    def test_candidate_slate_concurrent_seal_once_and_identical_idempotency(self):
        first = self._candidate()
        second = self._candidate(
            game_pk=777002,
            snapshot_id="snapshot-777002-4",
            sealed_snapshot_id="snapshot-777002-4",
            snapshot_hash="b" * 64,
            sealed_snapshot_hash="b" * 64,
        )

        def race(root, candidate_sets):
            barrier = Barrier(2)
            original_publish = protocols._publish_bundle

            def gated_publish(*args, **kwargs):
                try:
                    barrier.wait(timeout=0.25)
                except BrokenBarrierError:
                    pass
                return original_publish(*args, **kwargs)

            def seal(candidates):
                try:
                    return (
                        "ok",
                        protocols.write_prospective_candidate_slate(
                            root,
                            official_date="2026-07-13",
                            candidates=candidates,
                        ),
                    )
                except BaseException as exc:
                    return "error", exc

            with (
                patch(
                    "mlb_backtest.protocols._publish_bundle",
                    side_effect=gated_publish,
                ),
                self._trusted_clock("2026-07-13T22:26:00Z"),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                return tuple(executor.map(seal, candidate_sets))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            self._sealed_snapshot_ledger(root, game_pk=777002)
            first = self._disk_candidate(root, game_pk=777001)
            second = self._disk_candidate(root, game_pk=777002)
            results = race(root, ((first,), (second,)))
            successes = tuple(value for status, value in results if status == "ok")
            failures = tuple(value for status, value in results if status == "error")
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertRegex(str(failures[0]), "already sealed.*differs")
            loaded = protocols.load_prospective_candidate_slate(
                root, official_date="2026-07-13"
            )
            self.assertEqual(loaded, successes[0])
            slate_path = root / loaded.audit_path
            self.assertEqual(
                slate_path.read_bytes(),
                canonical_json_bytes(protocols._candidate_slate_raw(loaded)) + b"\n",
            )
            self.assertEqual(
                json.loads(slate_path.read_bytes())["candidates_sha256"],
                successes[0].candidates_sha256,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            first = self._disk_candidate(root, game_pk=777001)
            results = race(root, ((first,), (first,)))
            self.assertTrue(all(status == "ok" for status, _ in results), results)
            self.assertEqual(results[0][1], results[1][1])
            lock_path = (
                root
                / "audit/v5_2_qd/prospective_2026/"
                "candidate_slate_locks/2026-07-13.lock"
            )
            self.assertTrue(lock_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_candidate_slate_lock_rejects_symlink_and_hardlink_aliases(self):
        candidate = self._candidate()
        for alias_type in ("ancestor_symlink", "lock_hardlink", "root_symlink"):
            with (
                self.subTest(alias_type=alias_type),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary).resolve()
                if alias_type == "root_symlink":
                    real_root = base / "real" / "workspace"
                    real_root.mkdir(parents=True)
                    alias = base / "alias"
                    alias.symlink_to(base / "real", target_is_directory=True)
                    root = alias / "workspace"
                else:
                    root = base / "workspace"
                    lock_parent = (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "candidate_slate_locks"
                    )
                    lock_parent.parent.mkdir(parents=True)
                    if alias_type == "ancestor_symlink":
                        outside = base / "outside"
                        outside.mkdir()
                        lock_parent.symlink_to(outside, target_is_directory=True)
                    else:
                        lock_parent.mkdir()
                        target = base / "hardlink-target"
                        target.write_bytes(b"")
                        os.link(target, lock_parent / "2026-07-13.lock")
                with self.assertRaisesRegex(
                    ValueError, "symlink|hardlink|alias|ancestor"
                ):
                    protocols.write_prospective_candidate_slate(
                        root,
                        official_date="2026-07-13",
                        candidates=(candidate,),
                    )
                self.assertFalse(
                    (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "candidate_slates/2026-07-13.json"
                    ).exists()
                )

    def test_postlock_start_change_is_append_only_and_cannot_rewrite_lock(self):
        changes = load_prospective_spec(PROSPECTIVE_SPEC).raw["schedule_changes"]
        event = changes["postlock_start_change"]
        self.assertEqual(event["event"], "POSTLOCK_START_CHANGE")
        self.assertEqual(
            event["trigger"],
            "new_effective_start_utc_differs_from_previous_effective_start_utc",
        )
        self.assertEqual(event["write_mode"], "append_immutable_audit_event")
        self.assertEqual(
            event["latest_effective_start_utc_update"],
            "only_by_appending_POSTLOCK_START_CHANGE",
        )
        self.assertTrue(event["rewrite_forbidden"])
        self.assertTrue(event["delete_forbidden"])
        self.assertEqual(
            event["sealed_state_path_template"],
            "audit/v5_2_qd/prospective_2026/postlock_chain_state/{gamePk}.json",
        )
        self.assertEqual(
            event["state_persistence"],
            "atomic_event_and_sealed_state_publish_fsync_before_ack",
        )
        self.assertEqual(
            event["sealed_state_fields"],
            (
                "gamePk",
                "lock_basis_start_utc",
                "event_count",
                "head_hash",
                "persisted_at_utc",
                "audit_path",
                "event_log_path",
            ),
        )
        self.assertEqual(
            event["event_log_path_template"],
            "audit/v5_2_qd/prospective_2026/postlock_start_changes/{gamePk}.jsonl",
        )
        self.assertEqual(
            event["event_log_format"],
            "canonical_jsonl_one_POSTLOCK_START_CHANGE_per_line",
        )
        self.assertEqual(
            event["sealed_state_format"], "canonical_json_object_newline"
        )
        self.assertEqual(
            event["receipt_path_template"],
            (
                "audit/v5_2_qd/prospective_2026/postlock_chain_receipts/"
                "{gamePk}/{sequence:08d}-{head_hash}.json"
            ),
        )
        self.assertEqual(event["receipt_write"], "O_EXCL_fsync_read_only")
        self.assertEqual(
            event["game_lock_path_template"],
            "audit/v5_2_qd/prospective_2026/locks/{gamePk}.lock",
        )
        self.assertIn("external deletion", event["local_threat_boundary"])
        self.assertTrue(changes["relock_forbidden"])
        self.assertIn("lock_basis_start_utc", changes["immutable_fields"])

    def test_postlock_start_change_typed_hash_chain_records_every_transition(self):
        state = self._empty_chain_state()
        event, state = protocols.append_postlock_start_change(
            (),
            sealed_state=state,
            game_pk=777001,
            previous_effective_start_utc="2026-07-13T23:10:00Z",
            new_effective_start_utc="2026-07-13T23:40:00Z",
            observed_at_utc="2026-07-13T22:30:00Z",
            lock_basis_start_utc="2026-07-13T23:10:00Z",
            state_persisted_at_utc="2026-07-13T22:30:01Z",
        )
        events = (event,)
        event, state = protocols.append_postlock_start_change(
            events,
            sealed_state=state,
            game_pk=777001,
            previous_effective_start_utc="2026-07-13T23:40:00Z",
            new_effective_start_utc="2026-07-13T23:10:00Z",
            observed_at_utc="2026-07-13T22:35:00Z",
            lock_basis_start_utc="2026-07-13T23:10:00Z",
            state_persisted_at_utc="2026-07-13T22:35:01Z",
        )
        events = (*events, event)
        self.assertEqual(tuple(event.sequence for event in events), (1, 2))
        self.assertEqual(events[0].prior_event_hash, "0" * 64)
        self.assertEqual(events[1].prior_event_hash, events[0].event_hash)
        self.assertEqual(
            protocols.validate_postlock_start_changes(
                events,
                sealed_state=state,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
            ),
            events[-1].event_hash,
        )
        self.assertEqual(state.event_count, 2)
        self.assertEqual(state.head_hash, events[-1].event_hash)

    def test_postlock_chain_rejects_noop_rewrite_delete_and_wrong_lock(self):
        state = self._empty_chain_state()
        first, state = protocols.append_postlock_start_change(
            (),
            sealed_state=state,
            game_pk=777001,
            previous_effective_start_utc="2026-07-13T23:10:00Z",
            new_effective_start_utc="2026-07-13T23:40:00Z",
            observed_at_utc="2026-07-13T22:30:00Z",
            lock_basis_start_utc="2026-07-13T23:10:00Z",
            state_persisted_at_utc="2026-07-13T22:30:01Z",
        )
        events = (first,)
        second, state = protocols.append_postlock_start_change(
            events,
            sealed_state=state,
            game_pk=777001,
            previous_effective_start_utc="2026-07-13T23:40:00Z",
            new_effective_start_utc="2026-07-14T00:10:00Z",
            observed_at_utc="2026-07-13T22:35:00Z",
            lock_basis_start_utc="2026-07-13T23:10:00Z",
            state_persisted_at_utc="2026-07-13T22:35:01Z",
        )
        events = (*events, second)
        with self.assertRaisesRegex(ValueError, "effective start did not change"):
            protocols.append_postlock_start_change(
                events,
                sealed_state=state,
                game_pk=777001,
                previous_effective_start_utc="2026-07-14T00:10:00Z",
                new_effective_start_utc="2026-07-14T00:10:00Z",
                observed_at_utc="2026-07-13T22:36:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:36:01Z",
            )
        with self.assertRaisesRegex(ValueError, "before sealed lock"):
            protocols.append_postlock_start_change(
                (),
                sealed_state=self._empty_chain_state(),
                game_pk=777001,
                previous_effective_start_utc="2026-07-13T23:10:00Z",
                new_effective_start_utc="2026-07-13T23:40:00Z",
                observed_at_utc="2026-07-13T22:24:59Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:25:01Z",
            )
        rewritten = (*events[:-1], replace(events[-1], new_effective_start_utc="2026-07-13T23:20:00Z"))
        with self.assertRaisesRegex(ValueError, "event hash"):
            protocols.validate_postlock_start_changes(
                rewritten,
                sealed_state=state,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
            )
        with self.assertRaisesRegex(ValueError, "event count"):
            protocols.validate_postlock_start_changes(
                events[:-1],
                sealed_state=state,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
            )
        with self.assertRaisesRegex(ValueError, "lock basis"):
            protocols.validate_postlock_start_changes(
                events,
                sealed_state=state,
                lock_basis_start_utc="2026-07-13T23:11:00Z",
            )
        with self.assertRaisesRegex(ValueError, "lock basis"):
            protocols.validate_postlock_start_changes(
                (),
                sealed_state=self._empty_chain_state(),
                lock_basis_start_utc="2026-07-13T23:11:00Z",
            )
        for invalid_state in (
            replace(state, game_pk=True),
            replace(state, event_count=True),
        ):
            with self.assertRaisesRegex(ValueError, "exact positive integer|count"):
                protocols.validate_postlock_start_changes(
                    events,
                    sealed_state=invalid_state,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
        bool_event = replace(events[0], game_pk=True, sequence=True)
        with self.assertRaisesRegex(ValueError, "exact positive integers"):
            protocols.validate_postlock_start_changes(
                (bool_event, events[1]),
                sealed_state=state,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
            )

    def test_persisted_chain_initializes_and_appends_a_b_c_from_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            self.assertEqual(state.event_count, 0)
            transitions = (
                ("2026-07-13T23:40:00Z", "22:30"),
                ("2026-07-14T00:10:00Z", "22:35"),
            )
            for new, observed_hm in transitions:
                event, state = protocols.append_and_persist_postlock_start_change(
                    root,
                    game_pk=777001,
                    new_effective_start_utc=new,
                    observed_at_utc=f"2026-07-13T{observed_hm}:00Z",
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    state_persisted_at_utc=f"2026-07-13T{observed_hm}:01Z",
                )
                self.assertEqual(event.sequence, state.event_count)
            events, loaded = protocols.validate_persisted_postlock_chain(
                root, game_pk=777001
            )
            self.assertEqual(tuple(event.sequence for event in events), (1, 2))
            self.assertEqual(loaded, state)
            self.assertEqual(
                protocols.load_postlock_event_log(root, game_pk=777001), events
            )
            self.assertEqual(
                protocols.load_postlock_chain_state(root, game_pk=777001), state
            )

    def test_persisted_chain_detects_tail_delete_and_state_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            initial = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            state_path = root / initial.audit_path
            log_path = root / initial.event_log_path
            initial_state_bytes = state_path.read_bytes()
            protocols.append_and_persist_postlock_start_change(
                root,
                game_pk=777001,
                new_effective_start_utc="2026-07-13T23:40:00Z",
                observed_at_utc="2026-07-13T22:30:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:30:01Z",
            )
            first_log_bytes = log_path.read_bytes()
            first_state_bytes = state_path.read_bytes()
            protocols.append_and_persist_postlock_start_change(
                root,
                game_pk=777001,
                new_effective_start_utc="2026-07-14T00:10:00Z",
                observed_at_utc="2026-07-13T22:35:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:35:01Z",
            )
            full_log_bytes = log_path.read_bytes()
            full_state_bytes = state_path.read_bytes()

            log_path.write_bytes(first_log_bytes)
            with self.assertRaisesRegex(ValueError, "event count"):
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)
            log_path.write_bytes(full_log_bytes)
            state_path.write_bytes(initial_state_bytes)
            with self.assertRaisesRegex(ValueError, "event count"):
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)
            state_path.write_bytes(full_state_bytes)
            protocols.validate_persisted_postlock_chain(root, game_pk=777001)
            log_path.write_bytes(first_log_bytes)
            state_path.write_bytes(first_state_bytes)
            with self.assertRaisesRegex(ValueError, "receipt|anchor"):
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)
            log_path.write_bytes(full_log_bytes)
            state_path.write_bytes(full_state_bytes)

    def test_receipts_reject_overwrite_and_contiguous_gap(self):
        for mutation in ("overwrite", "gap"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                protocols.initialize_postlock_chain(
                    root,
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )
                for new, minute in (
                    ("2026-07-13T23:40:00Z", "30"),
                    ("2026-07-14T00:10:00Z", "35"),
                ):
                    protocols.append_and_persist_postlock_start_change(
                        root,
                        game_pk=777001,
                        new_effective_start_utc=new,
                        observed_at_utc=f"2026-07-13T22:{minute}:00Z",
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        state_persisted_at_utc=f"2026-07-13T22:{minute}:01Z",
                    )
                receipts = protocols.load_postlock_chain_receipts(
                    root, game_pk=777001
                )
                self.assertEqual(tuple(receipt.sequence for receipt in receipts), (0, 1, 2))
                receipt_dir = (
                    root
                    / "audit/v5_2_qd/prospective_2026/postlock_chain_receipts/777001"
                )
                receipt_paths = tuple(sorted(receipt_dir.glob("*.json")))
                self.assertTrue(
                    all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in receipt_paths)
                )
                if mutation == "overwrite":
                    receipt_paths[1].chmod(0o644)
                    receipt_paths[1].write_bytes(b"{}\n")
                    message = "receipt"
                else:
                    receipt_paths[1].unlink()
                    message = "gap"
                with self.assertRaisesRegex(ValueError, message):
                    protocols.validate_persisted_postlock_chain(root, game_pk=777001)

    def test_persisted_chain_rejects_missing_log_or_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "missing"):
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)
            state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            (root / state.audit_path).unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)

    def test_persisted_append_cannot_accept_older_in_memory_state_and_a_b_a_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old_state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            with self.assertRaises(TypeError):
                protocols.append_and_persist_postlock_start_change(
                    root,
                    sealed_state=old_state,
                    game_pk=777001,
                    new_effective_start_utc="2026-07-13T23:40:00Z",
                    observed_at_utc="2026-07-13T22:30:00Z",
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    state_persisted_at_utc="2026-07-13T22:30:01Z",
                )
            protocols.append_and_persist_postlock_start_change(
                root,
                game_pk=777001,
                new_effective_start_utc="2026-07-13T23:40:00Z",
                observed_at_utc="2026-07-13T22:30:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:30:01Z",
            )
            event, state = protocols.append_and_persist_postlock_start_change(
                root,
                game_pk=777001,
                new_effective_start_utc="2026-07-13T23:10:00Z",
                observed_at_utc="2026-07-13T22:35:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:35:01Z",
            )
            self.assertEqual(event.sequence, 2)
            self.assertEqual(state.event_count, 2)

    def test_persisted_append_rolls_back_publish_and_postcondition_failures(self):
        for failure in ("second_replace", "postcondition"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                state = protocols.initialize_postlock_chain(
                    root,
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )
                state_path, log_path = root / state.audit_path, root / state.event_log_path
                before = (log_path.read_bytes(), state_path.read_bytes())
                receipt_dir = (
                    root
                    / "audit/v5_2_qd/prospective_2026/"
                    "postlock_chain_receipts/777001"
                )
                before_receipts = tuple(
                    (path.name, path.read_bytes())
                    for path in sorted(receipt_dir.glob("*.json"))
                )
                if failure == "second_replace":
                    original_replace = protocols._replace_pinned
                    final_calls = 0

                    def fail_second(*args, **kwargs):
                        nonlocal final_calls
                        destination = kwargs["destination_path"]
                        if Path(destination) in (log_path, state_path):
                            final_calls += 1
                            if final_calls == 2:
                                raise OSError("state replace failed")
                        return original_replace(*args, **kwargs)

                    context = patch(
                        "mlb_backtest.protocols._replace_pinned", side_effect=fail_second
                    )
                    expected_error = OSError
                else:
                    original_validate = protocols.validate_persisted_postlock_chain
                    calls = 0

                    def fail_second_validation(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise ValueError("postcondition failed")
                        return original_validate(*args, **kwargs)

                    context = patch(
                        "mlb_backtest.protocols.validate_persisted_postlock_chain",
                        side_effect=fail_second_validation,
                    )
                    expected_error = ValueError
                with context, self.assertRaises(expected_error):
                    protocols.append_and_persist_postlock_start_change(
                        root,
                        game_pk=777001,
                        new_effective_start_utc="2026-07-13T23:40:00Z",
                        observed_at_utc="2026-07-13T22:30:00Z",
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        state_persisted_at_utc="2026-07-13T22:30:01Z",
                    )
                self.assertEqual((log_path.read_bytes(), state_path.read_bytes()), before)
                self.assertEqual(
                    tuple(
                        (path.name, path.read_bytes())
                        for path in sorted(receipt_dir.glob("*.json"))
                    ),
                    before_receipts,
                )
                self.assertEqual(list(root.rglob(".*.tmp")), [])
                protocols.validate_persisted_postlock_chain(root, game_pk=777001)
                event, retried = protocols.append_and_persist_postlock_start_change(
                    root,
                    game_pk=777001,
                    new_effective_start_utc="2026-07-13T23:40:00Z",
                    observed_at_utc="2026-07-13T22:30:00Z",
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    state_persisted_at_utc="2026-07-13T22:30:01Z",
                )
                self.assertEqual((event.sequence, retried.event_count), (1, 1))

    def test_initialize_postcondition_failure_removes_only_new_receipt_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            receipt_dir = (
                root
                / "audit/v5_2_qd/prospective_2026/"
                "postlock_chain_receipts/777001"
            )
            with patch(
                "mlb_backtest.protocols.validate_persisted_postlock_chain",
                side_effect=ValueError("initialization postcondition failed"),
            ):
                with self.assertRaisesRegex(ValueError, "postcondition"):
                    protocols.initialize_postlock_chain(
                        root,
                        game_pk=777001,
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        persisted_at_utc="2026-07-13T22:25:00Z",
                    )
            self.assertFalse(receipt_dir.exists())
            self.assertEqual(
                list(
                    root.glob(
                        "audit/v5_2_qd/prospective_2026/"
                        "postlock_start_changes/*.jsonl"
                    )
                ),
                [],
            )
            self.assertEqual(
                list(
                    root.glob(
                        "audit/v5_2_qd/prospective_2026/"
                        "postlock_chain_state/*.json"
                    )
                ),
                [],
            )
            self.assertEqual(list(root.rglob(".*.tmp")), [])
            state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            events, loaded = protocols.validate_persisted_postlock_chain(
                root, game_pk=777001
            )
            self.assertEqual(events, ())
            self.assertEqual(loaded, state)

    def test_receipt_creation_stage_failures_leave_no_orphan_and_retry(self):
        for operation in ("genesis", "append"):
            for stage in ("fchmod", "write", "fsync"):
                with (
                    self.subTest(operation=operation, stage=stage),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary).resolve()
                    if operation == "append":
                        prior_state = protocols.initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                    else:
                        prior_state = None
                    receipt_dir = (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "postlock_chain_receipts/777001"
                    )
                    prior_receipts = tuple(
                        (path.name, path.read_bytes(), path.stat().st_ino)
                        for path in sorted(receipt_dir.glob("*.json"))
                    )
                    original_fchmod = os.fchmod
                    original_write = os.write
                    original_fsync = os.fsync
                    receipt_identity = None
                    receipt_write_calls = 0
                    receipt_fsync_failed = False

                    def injected_fchmod(descriptor, mode):
                        nonlocal receipt_identity
                        if mode == 0o444:
                            metadata = os.fstat(descriptor)
                            receipt_identity = (metadata.st_dev, metadata.st_ino)
                            if stage == "fchmod":
                                raise OSError("injected receipt fchmod failure")
                        return original_fchmod(descriptor, mode)

                    def injected_write(descriptor, contents):
                        nonlocal receipt_write_calls
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        if stage == "write" and identity == receipt_identity:
                            receipt_write_calls += 1
                            if receipt_write_calls == 1:
                                partial = max(1, len(contents) // 3)
                                return original_write(descriptor, contents[:partial])
                            raise OSError("injected receipt write failure")
                        return original_write(descriptor, contents)

                    def injected_fsync(descriptor):
                        nonlocal receipt_fsync_failed
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        if (
                            stage == "fsync"
                            and identity == receipt_identity
                            and not receipt_fsync_failed
                        ):
                            receipt_fsync_failed = True
                            raise OSError("injected receipt fsync failure")
                        return original_fsync(descriptor)

                    with (
                        patch(
                            "mlb_backtest.protocols.os.fchmod",
                            side_effect=injected_fchmod,
                        ),
                        patch(
                            "mlb_backtest.protocols.os.write",
                            side_effect=injected_write,
                        ),
                        patch(
                            "mlb_backtest.protocols.os.fsync",
                            side_effect=injected_fsync,
                        ),
                        self.assertRaisesRegex(OSError, f"receipt {stage} failure"),
                    ):
                        if operation == "genesis":
                            protocols.initialize_postlock_chain(
                                root,
                                game_pk=777001,
                                lock_basis_start_utc="2026-07-13T23:10:00Z",
                                persisted_at_utc="2026-07-13T22:25:00Z",
                            )
                        else:
                            protocols.append_and_persist_postlock_start_change(
                                root,
                                game_pk=777001,
                                new_effective_start_utc="2026-07-13T23:40:00Z",
                                observed_at_utc="2026-07-13T22:30:00Z",
                                lock_basis_start_utc="2026-07-13T23:10:00Z",
                                state_persisted_at_utc="2026-07-13T22:30:01Z",
                            )
                    self.assertEqual(
                        tuple(
                            (path.name, path.read_bytes(), path.stat().st_ino)
                            for path in sorted(receipt_dir.glob("*.json"))
                        ),
                        prior_receipts,
                    )
                    self.assertEqual(list(root.rglob(".*.tmp")), [])
                    if operation == "genesis":
                        self.assertFalse(receipt_dir.exists())
                        self.assertFalse(receipt_dir.parent.exists())
                        retried = protocols.initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                        events, loaded = protocols.validate_persisted_postlock_chain(
                            root, game_pk=777001
                        )
                        self.assertEqual(events, ())
                    else:
                        events, loaded = protocols.validate_persisted_postlock_chain(
                            root, game_pk=777001
                        )
                        self.assertEqual(events, ())
                        self.assertEqual(loaded, prior_state)
                        _, retried = (
                            protocols.append_and_persist_postlock_start_change(
                                root,
                                game_pk=777001,
                                new_effective_start_utc="2026-07-13T23:40:00Z",
                                observed_at_utc="2026-07-13T22:30:00Z",
                                lock_basis_start_utc="2026-07-13T23:10:00Z",
                                state_persisted_at_utc="2026-07-13T22:30:01Z",
                            )
                        )
                    self.assertEqual(loaded, retried if operation == "genesis" else prior_state)

    def test_pre_return_receipt_cleanup_failure_preserves_path_rich_evidence(self):
        for operation in ("genesis", "append"):
            for stage in ("fchmod", "write", "fsync"):
                with (
                    self.subTest(operation=operation, stage=stage),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary).resolve()
                    if operation == "append":
                        prior_state = protocols.initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                    else:
                        prior_state = None
                    log_path = (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "postlock_start_changes/777001.jsonl"
                    )
                    state_path = (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "postlock_chain_state/777001.json"
                    )
                    prior_pair = (
                        (log_path.read_bytes(), state_path.read_bytes())
                        if operation == "append"
                        else None
                    )
                    receipt_dir = (
                        root
                        / "audit/v5_2_qd/prospective_2026/"
                        "postlock_chain_receipts/777001"
                    )
                    acknowledged = tuple(
                        (path.name, path.read_bytes(), path.stat().st_ino)
                        for path in sorted(receipt_dir.glob("*.json"))
                    )
                    original_fchmod = os.fchmod
                    original_write = os.write
                    original_fsync = os.fsync
                    receipt_identity = None
                    receipt_write_calls = 0
                    receipt_fsync_failed = False

                    def injected_fchmod(descriptor, mode):
                        nonlocal receipt_identity
                        if mode == 0o444:
                            metadata = os.fstat(descriptor)
                            receipt_identity = (metadata.st_dev, metadata.st_ino)
                            if stage == "fchmod":
                                raise OSError(
                                    "injected pre-return receipt fchmod failure"
                                )
                        return original_fchmod(descriptor, mode)

                    def injected_write(descriptor, contents):
                        nonlocal receipt_write_calls
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        if stage == "write" and identity == receipt_identity:
                            receipt_write_calls += 1
                            if receipt_write_calls == 1:
                                partial = max(1, len(contents) // 3)
                                return original_write(descriptor, contents[:partial])
                            raise OSError(
                                "injected pre-return receipt write failure"
                            )
                        return original_write(descriptor, contents)

                    def injected_fsync(descriptor):
                        nonlocal receipt_fsync_failed
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        if (
                            stage == "fsync"
                            and identity == receipt_identity
                            and not receipt_fsync_failed
                        ):
                            receipt_fsync_failed = True
                            raise OSError(
                                "injected pre-return receipt fsync failure"
                            )
                        return original_fsync(descriptor)

                    with (
                        patch(
                            "mlb_backtest.protocols.os.fchmod",
                            side_effect=injected_fchmod,
                        ),
                        patch(
                            "mlb_backtest.protocols.os.write",
                            side_effect=injected_write,
                        ),
                        patch(
                            "mlb_backtest.protocols.os.fsync",
                            side_effect=injected_fsync,
                        ),
                        patch(
                            "mlb_backtest.protocols._remove_created_postlock_receipt",
                            side_effect=OSError(
                                "injected pre-return receipt cleanup failure"
                            ),
                        ) as cleanup_receipt,
                        self.assertRaisesRegex(
                            RuntimeError, "protocol bundle rollback failed"
                        ) as raised,
                    ):
                        if operation == "genesis":
                            protocols.initialize_postlock_chain(
                                root,
                                game_pk=777001,
                                lock_basis_start_utc="2026-07-13T23:10:00Z",
                                persisted_at_utc="2026-07-13T22:25:00Z",
                            )
                        else:
                            protocols.append_and_persist_postlock_start_change(
                                root,
                                game_pk=777001,
                                new_effective_start_utc="2026-07-13T23:40:00Z",
                                observed_at_utc="2026-07-13T22:30:00Z",
                                lock_basis_start_utc="2026-07-13T23:10:00Z",
                                state_persisted_at_utc="2026-07-13T22:30:01Z",
                            )
                    if operation == "genesis":
                        self.assertFalse(log_path.exists())
                        self.assertFalse(state_path.exists())
                    else:
                        self.assertEqual(
                            (log_path.read_bytes(), state_path.read_bytes()),
                            prior_pair,
                        )
                        self.assertEqual(
                            protocols.load_postlock_event_log(
                                root, game_pk=777001
                            ),
                            (),
                        )
                        self.assertEqual(
                            protocols.load_postlock_chain_state(
                                root, game_pk=777001
                            ),
                            prior_state,
                        )
                    receipt_paths = tuple(sorted(receipt_dir.glob("*.json")))
                    self.assertEqual(len(receipt_paths), len(acknowledged) + 1)
                    self.assertEqual(
                        tuple(
                            (path.name, path.read_bytes(), path.stat().st_ino)
                            for path in receipt_paths[: len(acknowledged)]
                        ),
                        acknowledged,
                    )
                    orphan = receipt_paths[-1]
                    cleanup_receipt.assert_called_once()
                    self.assertEqual(
                        cleanup_receipt.call_args.args[0].path,
                        orphan,
                    )
                    recovery_files = tuple(sorted(root.rglob(".*.tmp")))
                    self.assertGreaterEqual(len(recovery_files), 2)
                    error_message = str(raised.exception)
                    self.assertIn(str(orphan), error_message)
                    self.assertIn(
                        f"injected pre-return receipt {stage} failure",
                        error_message,
                    )
                    self.assertIn(
                        "injected pre-return receipt cleanup failure",
                        error_message,
                    )
                    self.assertIn("recovery paths=", error_message)
                    self.assertTrue(
                        all(str(path) in error_message for path in recovery_files)
                    )
                    for path in recovery_files:
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())

    def test_exclusive_open_failure_removes_only_new_receipt_directories_and_retries(self):
        for operation in ("genesis", "append"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                if operation == "append":
                    prior_state = protocols.initialize_postlock_chain(
                        root,
                        game_pk=777001,
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        persisted_at_utc="2026-07-13T22:25:00Z",
                    )
                else:
                    prior_state = None
                receipt_root = (
                    root
                    / "audit/v5_2_qd/prospective_2026/postlock_chain_receipts"
                )
                receipt_dir = receipt_root / "777001"
                prior_directory_inode = (
                    receipt_dir.stat().st_ino if receipt_dir.exists() else None
                )
                prior_receipts = tuple(
                    (path.name, path.read_bytes(), path.stat().st_ino)
                    for path in sorted(receipt_dir.glob("*.json"))
                )
                original_open = os.open

                def fail_receipt_open(path, flags, *args, **kwargs):
                    if (
                        flags & os.O_EXCL
                        and "postlock_chain_receipts" in str(path)
                        and str(path).endswith(".json")
                    ):
                        raise OSError("injected receipt exclusive open failure")
                    return original_open(path, flags, *args, **kwargs)

                with (
                    patch(
                        "mlb_backtest.protocols.os.open",
                        side_effect=fail_receipt_open,
                    ),
                    self.assertRaisesRegex(OSError, "exclusive open failure"),
                ):
                    if operation == "genesis":
                        protocols.initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                    else:
                        protocols.append_and_persist_postlock_start_change(
                            root,
                            game_pk=777001,
                            new_effective_start_utc="2026-07-13T23:40:00Z",
                            observed_at_utc="2026-07-13T22:30:00Z",
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            state_persisted_at_utc="2026-07-13T22:30:01Z",
                        )
                if operation == "genesis":
                    self.assertFalse(receipt_root.exists())
                    self.assertEqual(
                        list(
                            root.glob(
                                "audit/v5_2_qd/prospective_2026/"
                                "postlock_start_changes/*.jsonl"
                            )
                        ),
                        [],
                    )
                    self.assertEqual(
                        list(
                            root.glob(
                                "audit/v5_2_qd/prospective_2026/"
                                "postlock_chain_state/*.json"
                            )
                        ),
                        [],
                    )
                    retried = protocols.initialize_postlock_chain(
                        root,
                        game_pk=777001,
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        persisted_at_utc="2026-07-13T22:25:00Z",
                    )
                    events, loaded = protocols.validate_persisted_postlock_chain(
                        root, game_pk=777001
                    )
                    self.assertEqual(events, ())
                    self.assertEqual(loaded, retried)
                else:
                    self.assertEqual(receipt_dir.stat().st_ino, prior_directory_inode)
                    self.assertEqual(
                        tuple(
                            (path.name, path.read_bytes(), path.stat().st_ino)
                            for path in sorted(receipt_dir.glob("*.json"))
                        ),
                        prior_receipts,
                    )
                    events, loaded = protocols.validate_persisted_postlock_chain(
                        root, game_pk=777001
                    )
                    self.assertEqual(events, ())
                    self.assertEqual(loaded, prior_state)
                    event, retried = (
                        protocols.append_and_persist_postlock_start_change(
                            root,
                            game_pk=777001,
                            new_effective_start_utc="2026-07-13T23:40:00Z",
                            observed_at_utc="2026-07-13T22:30:00Z",
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            state_persisted_at_utc="2026-07-13T22:30:01Z",
                        )
                    )
                    self.assertEqual((event.sequence, retried.event_count), (1, 1))

    def test_exclusive_open_directory_cleanup_failure_is_path_rich(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            receipt_root = (
                root / "audit/v5_2_qd/prospective_2026/postlock_chain_receipts"
            )
            receipt_dir = receipt_root / "777001"
            original_open = os.open

            def fail_receipt_open(path, flags, *args, **kwargs):
                if (
                    flags & os.O_EXCL
                    and "postlock_chain_receipts" in str(path)
                    and str(path).endswith(".json")
                ):
                    raise OSError("injected receipt exclusive open failure")
                return original_open(path, flags, *args, **kwargs)

            with (
                patch(
                    "mlb_backtest.protocols.os.open",
                    side_effect=fail_receipt_open,
                ),
                patch(
                    "mlb_backtest.protocols._remove_created_receipt_directories",
                    side_effect=OSError("injected receipt directory cleanup failure"),
                    create=True,
                ),
                self.assertRaisesRegex(
                    RuntimeError, "exclusive open recovery failed"
                ) as raised,
            ):
                protocols.initialize_postlock_chain(
                    root,
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )
            self.assertTrue(receipt_dir.is_dir())
            error_message = str(raised.exception)
            self.assertIn("injected receipt exclusive open failure", error_message)
            self.assertIn("injected receipt directory cleanup failure", error_message)
            self.assertIn(str(receipt_dir), error_message)
            self.assertIn("recovery paths=", error_message)

    def test_receipt_removal_failure_is_path_rich_and_preserves_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            state_path, log_path = root / state.audit_path, root / state.event_log_path
            before = (log_path.read_bytes(), state_path.read_bytes())
            original_validate = protocols.validate_persisted_postlock_chain
            calls = 0

            def fail_second_validation(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError("postcondition failed after receipt creation")
                return original_validate(*args, **kwargs)

            expected_receipt_dir = (
                root
                / "audit/v5_2_qd/prospective_2026/"
                "postlock_chain_receipts/777001"
            )
            with (
                patch(
                    "mlb_backtest.protocols.validate_persisted_postlock_chain",
                    side_effect=fail_second_validation,
                ),
                patch(
                    "mlb_backtest.protocols._remove_created_postlock_receipt",
                    side_effect=OSError("injected receipt unlink failure"),
                    create=True,
                ),
                self.assertRaisesRegex(RuntimeError, "rollback failed") as raised,
            ):
                protocols.append_and_persist_postlock_start_change(
                    root,
                    game_pk=777001,
                    new_effective_start_utc="2026-07-13T23:40:00Z",
                    observed_at_utc="2026-07-13T22:30:00Z",
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    state_persisted_at_utc="2026-07-13T22:30:01Z",
                )
            self.assertEqual((log_path.read_bytes(), state_path.read_bytes()), before)
            self.assertEqual(len(tuple(expected_receipt_dir.glob("*.json"))), 2)
            recovery_files = tuple(sorted(root.rglob(".*.tmp")))
            self.assertTrue(recovery_files)
            error_message = str(raised.exception)
            self.assertIn("postlock_chain_receipts/777001", error_message)
            self.assertIn("injected receipt unlink failure", error_message)
            self.assertIn("recovery paths=", error_message)
            self.assertTrue(all(str(path) in error_message for path in recovery_files))

    def test_concurrent_appends_are_serialized_without_acknowledged_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )

            def append(new_start):
                return protocols.append_and_persist_postlock_start_change(
                    root,
                    game_pk=777001,
                    new_effective_start_utc=new_start,
                    observed_at_utc="2026-07-13T22:30:00Z",
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    state_persisted_at_utc="2026-07-13T22:30:01Z",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(
                        append,
                        ("2026-07-13T23:40:00Z", "2026-07-14T00:10:00Z"),
                    )
                )
            events, state = protocols.validate_persisted_postlock_chain(
                root, game_pk=777001
            )
            self.assertEqual(state.event_count, 2)
            self.assertEqual({event.sequence for event, _ in results}, {1, 2})
            self.assertEqual(
                {event.new_effective_start_utc for event in events},
                {"2026-07-13T23:40:00Z", "2026-07-14T00:10:00Z"},
            )
            self.assertEqual(
                events[1].previous_effective_start_utc,
                events[0].new_effective_start_utc,
            )

    def test_postlock_rejects_symlink_in_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            real = base / "real"
            workspace = real / "workspace"
            workspace.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "ancestor.*symlink|symlink.*ancestor"):
                protocols.initialize_postlock_chain(
                    alias / "workspace",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )

    def test_persisted_chain_rejects_aliases_and_preserves_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = protocols.initialize_postlock_chain(
                root,
                game_pk=777001,
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                persisted_at_utc="2026-07-13T22:25:00Z",
            )
            state_path, log_path = root / state.audit_path, root / state.event_log_path
            self.assertEqual(
                (stat.S_IMODE(log_path.stat().st_mode), stat.S_IMODE(state_path.stat().st_mode)),
                (0o644, 0o644),
            )
            log_path.chmod(0o600)
            state_path.chmod(0o601)
            protocols.append_and_persist_postlock_start_change(
                root,
                game_pk=777001,
                new_effective_start_utc="2026-07-13T23:40:00Z",
                observed_at_utc="2026-07-13T22:30:00Z",
                lock_basis_start_utc="2026-07-13T23:10:00Z",
                state_persisted_at_utc="2026-07-13T22:30:01Z",
            )
            self.assertEqual(
                (stat.S_IMODE(log_path.stat().st_mode), stat.S_IMODE(state_path.stat().st_mode)),
                (0o600, 0o601),
            )
            hardlink = state_path.with_name("state-hardlink.json")
            os.link(state_path, hardlink)
            with self.assertRaisesRegex(ValueError, "alias"):
                protocols.load_postlock_chain_state(root, game_pk=777001)
            hardlink.unlink()
            real_log = log_path.with_name("real.jsonl")
            log_path.replace(real_log)
            log_path.symlink_to(real_log)
            with self.assertRaisesRegex(ValueError, "symlink|alias"):
                protocols.load_postlock_event_log(root, game_pk=777001)

    def test_prospective_oracle_is_recursive_frozen_detached_and_design_pinned(self):
        oracle = protocols.PROSPECTIVE_LITERAL_ORACLE
        with self.assertRaises(TypeError):
            oracle["polling"]["max_ntp_offset_seconds"] = 3
        first = expected_prospective_spec()
        first["polling"]["max_ntp_offset_seconds"] = 3
        second = expected_prospective_spec()
        self.assertEqual(second["polling"]["max_ntp_offset_seconds"], 2)
        self.assertEqual(
            second["freeze_pin"]["approved_design_blob_sha256"],
            protocols.CURRENT_APPROVED_DESIGN_SHA256,
        )
        self.assertEqual(
            second["freeze_pin"]["approved_design_commit"],
            protocols.CURRENT_APPROVED_DESIGN_COMMIT,
        )
        self.assertEqual(
            protocols.PROSPECTIVE_CONTRACT_CANONICAL_SHA256,
            "d4b8fa399c5dc8138aff961fdbd0cb4fc92facdfb4dc9ae4e820a486ce4bcc5e",
        )
        with self.assertRaisesRegex(ValueError, "prospective spec differs"):
            parse_prospective_spec(first)
        tampered = copy.deepcopy(second)
        tampered["freeze_pin"]["approved_design_blob_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "design pin"):
            parse_prospective_spec(tampered)
        with patch(
            "mlb_backtest.protocols.PROSPECTIVE_CONTRACT_CANONICAL_SHA256",
            "0" * 64,
        ):
            with self.assertRaisesRegex(ValueError, "canonical digest"):
                parse_prospective_spec(second)

    def test_prospective_writer_verifies_approved_design_blob_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            with patch(
                "mlb_backtest.protocols.approved_design_bytes",
                return_value=b"tampered approved design",
            ):
                with self.assertRaisesRegex(ValueError, "design pin"):
                    write_prospective_contract(root)
            self.assertFalse(root.exists())

    def test_prospective_outcomes_are_inaccessible_until_all_locks_are_sealed(self):
        isolation = load_prospective_spec(PROSPECTIVE_SPEC).raw["outcome_isolation"]
        self.assertEqual(
            isolation["locked_namespace"],
            "data/v5_2_qd/prospective_2026/locked",
        )
        self.assertEqual(
            isolation["outcomes_namespace"],
            "data/v5_2_qd/prospective_2026/outcomes",
        )
        self.assertFalse(isolation["feature_and_lock_import_outcomes"])
        self.assertFalse(
            isolation["prediction_ranking_lock_outcome_store_access"]
        )
        self.assertTrue(
            isolation["join_after_all_final_verified_candidate_locks_sealed"]
        )
        self.assertTrue(isolation["locked_prediction_rewrite_forbidden"])

    def test_official_ranking_is_frozen_verified_eligible_only_and_post_lock(self):
        pool = load_prospective_spec(PROSPECTIVE_SPEC).raw["official_pool"]
        self.assertTrue(pool["eligible_only"])
        self.assertEqual(
            pool["ranking"],
            (
                "pick_probability_desc",
                "data_completeness_score_desc",
                "absolute_starter_edge_desc",
                "lock_basis_start_utc_asc",
                "gamePk_asc",
            ),
        )
        self.assertEqual(
            pool["ranking_after"],
            "all_final_verified_t45_candidate_locks_sealed",
        )
        self.assertFalse(pool["incomplete_or_unverified_rows_affect_rank"])
        self.assertFalse(pool["partial_rows_affect_rank"])
        self.assertEqual(pool["candidate_date_state"], "CANDIDATE_ACTIVE")
        self.assertEqual(
            pool["selection_gate"],
            "all_candidate_locks_sealed_and_final_lock_receipt_recorded",
        )
        self.assertTrue(pool["candidate_top1_preclaim_forbidden"])
        self.assertEqual(
            pool["slate_manifest_path_template"],
            "audit/v5_2_qd/prospective_2026/candidate_slates/{official_date}.json",
        )
        self.assertEqual(
            pool["slate_lock_path_template"],
            "audit/v5_2_qd/prospective_2026/"
            "candidate_slate_locks/{official_date}.lock",
        )
        self.assertEqual(
            pool["slate_seal_policy"],
            "immutable_sealed_once_idempotent_exact_same_manifest",
        )
        self.assertEqual(
            pool["all_locks_sealed"], "derived_from_every_bound_candidate_lock"
        )
        self.assertEqual(
            pool["final_lock_receipt_at_utc"], "max_candidate_lock_receipt_at_utc"
        )

    def test_new_isolation_ranking_and_postlock_literals_reject_tampering(self):
        raw = json.loads(PROSPECTIVE_SPEC.read_text())
        mutations = (
            lambda value: value["schedule_changes"]["postlock_start_change"].__setitem__(
                "rewrite_forbidden", False
            ),
            lambda value: value["outcome_isolation"].__setitem__(
                "feature_and_lock_import_outcomes", True
            ),
            lambda value: value["official_pool"]["ranking"].reverse(),
            lambda value: value["official_pool"].__setitem__("eligible_only", False),
        )
        for mutate in mutations:
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "prospective spec differs"):
                    parse_prospective_spec(changed)

    def test_evidence_gate_is_exact_and_historical_rows_are_forbidden(self):
        gate = load_prospective_spec(PROSPECTIVE_SPEC).raw["evidence_gate"]
        self.assertTrue(gate["all_integrity_gates_pass"])
        self.assertEqual(
            gate["accuracy_gt_home"],
            {"required": True, "exact_denominator": 500},
        )
        self.assertEqual(
            gate["accuracy_gt_d1_season_record"],
            {"required": True, "exact_denominator": "N_season"},
        )
        self.assertTrue(gate["season_baseline_lower_bound_positive"])
        self.assertEqual(gate["season_baseline_exact_mcnemar_p_lt"], 0.05)
        self.assertFalse(gate["historical_rows_permitted"])

    def test_schema_const_and_every_nested_object_are_closed(self):
        raw = json.loads(PROSPECTIVE_SPEC.read_text())
        schema = json.loads(PROSPECTIVE_SCHEMA.read_text())
        self.assertEqual(schema["const"], raw)
        self.assertEqual(set(schema["required"]), PROSPECTIVE_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), PROSPECTIVE_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])

        def assert_closed(value, node, path):
            if isinstance(value, dict):
                self.assertEqual(set(node["required"]), set(value), path)
                self.assertEqual(set(node["properties"]), set(value), path)
                self.assertFalse(node["additionalProperties"], path)
                for key, child in value.items():
                    assert_closed(child, node["properties"][key], f"{path}.{key}")
            elif isinstance(value, list) and value:
                for index, child in enumerate(value):
                    assert_closed(child, node["items"], f"{path}[{index}]")

        assert_closed(raw, schema, "prospective")

    def test_writer_is_canonical_byte_deterministic_and_preserves_modes(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            roots = (Path(first), Path(second))
            for index, root in enumerate(roots):
                if index == 0:
                    for relative in (
                        Path("protocols/v5_2_qd/prospective_2026.json"),
                        Path("schemas/v5_2_prospective.schema.json"),
                    ):
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"old")
                        path.chmod(0o600)
                write_prospective_contract(root)
            for relative in (
                "protocols/v5_2_qd/prospective_2026.json",
                "schemas/v5_2_prospective.schema.json",
            ):
                first_path, second_path = roots[0] / relative, roots[1] / relative
                self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
                self.assertEqual(
                    first_path.read_bytes(),
                    canonical_json_bytes(json.loads(first_path.read_bytes())) + b"\n",
                )
                self.assertEqual(stat.S_IMODE(first_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(second_path.stat().st_mode), 0o644)

    def test_writer_rolls_back_both_files_on_second_replace_failure(self):
        relatives = (
            Path("protocols/v5_2_qd/prospective_2026.json"),
            Path("schemas/v5_2_prospective.schema.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old = {}
            for index, relative in enumerate(relatives):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                old[path] = f"old-{index}".encode()
                path.write_bytes(old[path])
            original_replace = protocols._replace_pinned
            final_replacements = 0

            def fail_second_final(*args, **kwargs):
                nonlocal final_replacements
                destination = kwargs["destination_path"]
                if Path(destination) in tuple(root / item for item in relatives):
                    final_replacements += 1
                    if final_replacements == 2:
                        raise OSError("second prospective replace failed")
                return original_replace(*args, **kwargs)

            with patch(
                "mlb_backtest.protocols._replace_pinned", side_effect=fail_second_final
            ):
                with self.assertRaisesRegex(
                    OSError, "second prospective replace failed"
                ):
                    write_prospective_contract(root)
            for path, contents in old.items():
                self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(list(root.rglob(".*.tmp")), [])

    def test_writer_preserves_recovery_files_when_rollback_restore_fails(self):
        relatives = (
            Path("protocols/v5_2_qd/prospective_2026.json"),
            Path("schemas/v5_2_prospective.schema.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old = {}
            for index, relative in enumerate(relatives):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                old[path] = f"irreplaceable-old-{index}".encode()
                path.write_bytes(old[path])
            original_replace = protocols._replace_pinned
            final_calls = 0

            def fail_publish_then_restore(*args, **kwargs):
                nonlocal final_calls
                destination = kwargs["destination_path"]
                if Path(destination) in tuple(root / item for item in relatives):
                    final_calls += 1
                    if final_calls == 2:
                        raise OSError("publish replacement failed")
                    if final_calls == 3:
                        raise OSError("rollback restoration failed")
                return original_replace(*args, **kwargs)

            with patch(
                "mlb_backtest.protocols._replace_pinned",
                side_effect=fail_publish_then_restore,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "rollback failed.*recovery paths="
                ) as raised:
                    write_prospective_contract(root)
            recovery_files = tuple(sorted(root.rglob(".*.tmp")))
            self.assertTrue(recovery_files)
            message = str(raised.exception)
            for path in recovery_files:
                self.assertIn(str(path), message)
            recovered_bytes = {path.read_bytes() for path in recovery_files}
            self.assertIn(old[root / relatives[0]], recovered_bytes)
            self.assertEqual((root / relatives[1]).read_bytes(), old[root / relatives[1]])


class ProtocolBundlePathSecurityTest(unittest.TestCase):
    def test_contract_writers_reject_symlinked_roots_without_writing_outside(self):
        writers = (
            ("config", lambda root: write_v52_config_contracts(root, approved_design_root=ROOT)),
            ("historical", write_historical_contract),
            ("prospective", lambda root: write_prospective_contract(root, approved_design_root=ROOT)),
        )
        for label, writer in writers:
            with self.subTest(writer=label), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                outside = base / "outside"
                outside.mkdir()
                linked_root = base / "linked-root"
                linked_root.symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(ValueError, "symlink"):
                    writer(linked_root)

                self.assertEqual(list(outside.rglob("*")), [])

    def test_contract_writers_reject_symlinked_output_directories(self):
        cases = (
            (
                "config",
                "config",
                lambda root: write_v52_config_contracts(root, approved_design_root=ROOT),
            ),
            ("historical", "protocols", write_historical_contract),
            (
                "prospective",
                "protocols",
                lambda root: write_prospective_contract(root, approved_design_root=ROOT),
            ),
        )
        for label, linked_name, writer in cases:
            with self.subTest(writer=label), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "root"
                outside = base / "outside"
                root.mkdir()
                outside.mkdir()
                (root / linked_name).symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(ValueError, "symlink"):
                    writer(root)

                self.assertEqual(list(outside.rglob("*")), [])

    def test_contract_writer_rejects_symlink_in_earlier_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            real = base / "real"
            root = real / "container/workspace"
            root.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(real, target_is_directory=True)
            aliased_root = alias / "container/workspace"

            with self.assertRaisesRegex(ValueError, "symlink"):
                write_historical_contract(aliased_root)

            self.assertEqual(list(root.rglob("*")), [])

    def test_publisher_pins_directory_during_symlink_swap_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            protocol_dir = root / "protocols/v5_2_qd"
            protocol_dir.mkdir(parents=True)
            (root / "schemas").mkdir()
            outside.mkdir()
            historical = protocol_dir / "historical_walkforward_500.json"
            historical.write_bytes(b"sealed-old")
            original_replace = protocols._replace_pinned
            swapped = False

            def swap_before_replace(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    (root / "protocols").rename(root / "protocols-pinned")
                    (root / "protocols").symlink_to(outside, target_is_directory=True)
                return original_replace(*args, **kwargs)

            with (
                patch(
                    "mlb_backtest.protocols._replace_pinned",
                    side_effect=swap_before_replace,
                ),
                self.assertRaisesRegex(ValueError, "directory identity changed"),
            ):
                write_historical_contract(root)

            self.assertEqual(list(outside.rglob("*")), [])
            restored = (
                root
                / "protocols-pinned/v5_2_qd/historical_walkforward_500.json"
            )
            self.assertEqual(restored.read_bytes(), b"sealed-old")


class HistoricalRunSpecTest(unittest.TestCase):
    def test_exact_folds_and_oof_sequences(self):
        run = load_run_spec(HISTORICAL_SPEC)
        self.assertEqual(run.target_count, 500)
        self.assertEqual(run.allowed_target_years, (2022, 2023, 2024))
        self.assertEqual(run.forbidden_target_years, (2025,))
        self.assertEqual(run.folds[0].fold_id, "H22")
        self.assertEqual(run.folds[0].normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(
            run.folds[0].oof_calibration,
            (
                (2019, (2018,)),
                (2020, (2018, 2019)),
                (2021, (2018, 2019, 2020)),
            ),
        )
        self.assertEqual(
            run.folds[2].normalization_years,
            (2018, 2019, 2020, 2021, 2022, 2023),
        )

    def test_future_and_2025_injections_fail_closed(self):
        run = load_run_spec(HISTORICAL_SPEC)
        with self.assertRaisesRegex(ValueError, "future season"):
            validate_fold(replace(run.folds[0], normalization_years=(2018, 2022)))
        with self.assertRaisesRegex(ValueError, "2025 is excluded"):
            validate_run(
                replace(
                    run,
                    allowed_target_years=(2022, 2023, 2024, 2025),
                )
            )

    def test_run_and_fold_schema_keys_mirror_strict_parser(self):
        schema = json.loads(RUN_SCHEMA.read_text())
        self.assertEqual(set(schema["required"]), RUN_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), RUN_REQUIRED_KEYS)
        fold_schema = schema["$defs"]["fold"]
        self.assertEqual(set(fold_schema["required"]), FOLD_REQUIRED_KEYS)
        self.assertEqual(set(fold_schema["properties"]), FOLD_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(fold_schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["folds"],
            {"type": "array", "items": {"$ref": "#/$defs/fold"}},
        )

    def test_run_contract_literals(self):
        run = load_run_spec(HISTORICAL_SPEC)
        self.assertEqual(run.raw["date_queue"], HISTORICAL_DATE_QUEUE)
        self.assertEqual(run.raw["selection"], HISTORICAL_SELECTION)
        self.assertEqual(run.raw["terminal_contract"], HISTORICAL_TERMINAL)
        self.assertEqual(
            run.raw["date_state_precedence"],
            ("RUN_INVALID", "SYSTEM_MISS", "NO_ELIGIBLE_DATE", "LOCK_TOP1"),
        )
        self.assertEqual(
            run.raw["evidence_labels"],
            (
                "RUN_INVALID",
                "ROBUSTNESS_NOT_SUPPORTED",
                "ROBUSTNESS_SUPPORTED_NOT_CONFIRMED",
            ),
        )
        self.assertEqual(run.raw["paired_denominators"], HISTORICAL_DENOMINATORS)
        self.assertEqual(run.raw["outcome_isolation"], HISTORICAL_OUTCOME_ISOLATION)

    def test_schema_const_and_parser_reject_every_frozen_change(self):
        raw = json.loads(HISTORICAL_SPEC.read_text())
        schema = json.loads(RUN_SCHEMA.read_text())
        self.assertEqual(schema["const"], raw)
        changed = copy.deepcopy(raw)
        changed["selection"]["graded_target"] = 499
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["folds"][0]["oof_calibration"][0]["normalization_years"] = [2017]
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)

    def test_writer_is_canonical_and_byte_deterministic(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            root_a, root_b = Path(first), Path(second)
            write_historical_contract(root_a)
            write_historical_contract(root_b)
            for relative in (
                "protocols/v5_2_qd/historical_walkforward_500.json",
                "schemas/v5_2_run_spec.schema.json",
            ):
                first_bytes = (root_a / relative).read_bytes()
                self.assertEqual(first_bytes, (root_b / relative).read_bytes())
                self.assertEqual(
                    first_bytes,
                    canonical_json_bytes(json.loads(first_bytes)) + b"\n",
                )

    def test_parser_rejects_unknown_missing_and_type_confusion(self):
        raw = json.loads(HISTORICAL_SPEC.read_text())
        changed = copy.deepcopy(raw)
        changed["unknown"] = None
        with self.assertRaisesRegex(ValueError, "keys"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["folds"][0].pop("latest_training_date")
        with self.assertRaisesRegex(ValueError, "keys"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["target_count"] = True
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)

    def test_fold_literal_oracle_is_recursive_frozen_and_detached(self):
        with self.assertRaises(TypeError):
            FOLD_LITERALS[0]["normalization_years"] = (2017,)
        first = expected_historical_run()
        first["folds"][0]["normalization_years"][0] = 2017
        second = expected_historical_run()
        self.assertEqual(second["folds"][0]["normalization_years"][0], 2018)
        parse_run_spec(second)

    def test_public_fold_validator_rejects_chronology_and_typed_raw_drift(self):
        fold = load_run_spec(HISTORICAL_SPEC).folds[0]
        invalid = (
            (replace(fold, target_dates=("2022-4-07", "2022-10-05")), "canonical ISO"),
            (
                replace(fold, target_dates=("2022-10-05", "2022-04-07")),
                "start must not follow",
            ),
            (replace(fold, target_dates=("2021-04-07", "2022-10-05")), "target year"),
            (replace(fold, latest_training_date="2022-04-07"), "latest training"),
            (
                replace(fold, normalization_years=(2018, 2020, 2019, 2021)),
                "strictly ascending",
            ),
            (
                replace(fold, normalization_years=(2018, 2019, 2020, 2020)),
                "strictly ascending",
            ),
            (replace(fold, normalization_years=(2019, 2020, 2021)), "exact past range"),
            (
                replace(
                    fold,
                    oof_calibration=(
                        (2020, (2018, 2019)),
                        (2019, (2018,)),
                        (2021, (2018, 2019, 2020)),
                    ),
                ),
                "OOF target years",
            ),
            (
                replace(
                    fold,
                    oof_calibration=(
                        (2019, (2018,)),
                        (2020, (2018,)),
                        (2021, (2018, 2019, 2020)),
                    ),
                ),
                "rolling-origin",
            ),
            (replace(fold, fold_id="H99"), "unknown fold"),
        )
        for changed, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_fold(changed)

        raw = copy.deepcopy(json.loads(HISTORICAL_SPEC.read_text())["folds"][0])
        raw["latest_training_date"] = "2020-12-31"
        with self.assertRaisesRegex(ValueError, "raw differs"):
            validate_fold(replace(fold, raw=raw))

    def test_public_run_validator_rejects_duplicate_and_typed_raw_drift(self):
        run = load_run_spec(HISTORICAL_SPEC)
        with self.assertRaisesRegex(ValueError, "fold identity fields are invalid"):
            validate_run(replace(run, folds=(replace(run.folds[0], fold_id=[]),)))
        with self.assertRaisesRegex(ValueError, "fold IDs must be unique"):
            validate_run(replace(run, folds=(run.folds[0], run.folds[0], run.folds[2])))
        duplicate_target = replace(run.folds[1], fold_id="H23-copy", target_year=2022)
        with self.assertRaisesRegex(ValueError, "fold target years must be unique"):
            validate_run(
                replace(run, folds=(run.folds[0], duplicate_target, run.folds[2]))
            )
        with self.assertRaisesRegex(ValueError, "typed fields differ from raw"):
            validate_run(replace(run, target_count=499))

    def test_historical_writer_rolls_back_two_file_bundle(self):
        relative_paths = (
            Path("protocols/v5_2_qd/historical_walkforward_500.json"),
            Path("schemas/v5_2_run_spec.schema.json"),
        )
        for initial_state in ((True, True), (False, False), (True, False)):
            with (
                self.subTest(initial_state=initial_state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                old = {}
                modes = {}
                for index, (relative, preexisting) in enumerate(
                    zip(relative_paths, initial_state, strict=True)
                ):
                    if preexisting:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        old[path] = f"old-{index}".encode()
                        modes[path] = 0o600 + index
                        path.write_bytes(old[path])
                        path.chmod(modes[path])
                original_replace = protocols._replace_pinned
                final_replacements = 0

                def fail_second_final(*args, **kwargs):
                    nonlocal final_replacements
                    destination = kwargs["destination_path"]
                    if Path(destination) in tuple(
                        root / item for item in relative_paths
                    ):
                        final_replacements += 1
                        if final_replacements == 2:
                            raise OSError("second historical replace failed")
                    return original_replace(*args, **kwargs)

                with patch(
                    "mlb_backtest.protocols._replace_pinned",
                    side_effect=fail_second_final,
                ):
                    with self.assertRaisesRegex(
                        OSError, "second historical replace failed"
                    ):
                        write_historical_contract(root)
                for relative, preexisting in zip(
                    relative_paths, initial_state, strict=True
                ):
                    path = root / relative
                    if preexisting:
                        self.assertEqual(path.read_bytes(), old[path])
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[path])
                    else:
                        self.assertFalse(path.exists())
                self.assertEqual(list(root.rglob(".*.tmp")), [])

    def test_historical_writer_preserves_modes_and_defaults_new_files_to_0644(self):
        relatives = (
            Path("protocols/v5_2_qd/historical_walkforward_500.json"),
            Path("schemas/v5_2_run_spec.schema.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, relative in enumerate(relatives):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old")
                path.chmod(0o600 + index)
            write_historical_contract(root)
            self.assertEqual(
                tuple(stat.S_IMODE((root / item).stat().st_mode) for item in relatives),
                (0o600, 0o601),
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_historical_contract(root)
            self.assertEqual(
                tuple(stat.S_IMODE((root / item).stat().st_mode) for item in relatives),
                (0o644, 0o644),
            )


class V52ConfigTest(unittest.TestCase):
    def test_v52_preserves_prediction_structure_but_has_no_fitted_values(self):
        config = load_v52_config(V52_CONFIG)
        v51 = json.loads((ROOT / "config/v5_1_bt.json").read_text())
        self.assertEqual(config.component_weights, v51["component_weights"])
        self.assertEqual(config.shrinkage_constants, v51["shrinkage_constants"])
        self.assertEqual(config.probability_clip, (0.10, 0.90))
        self.assertNotIn("normalization", config.raw)
        self.assertNotIn("slot_weights", config.raw)
        serialized = canonical_json_bytes(config.raw).decode("utf-8")
        self.assertNotIn('"alpha"', serialized)
        self.assertNotIn('"beta"', serialized)
        self.assertNotIn("fitted_model_cards", serialized)

    def test_expected_config_cannot_mutate_the_validation_oracle(self):
        first = expected_v52_config()
        first["internal_metrics"]["starter"]["season_xwoba"]["weight"] = 0.99
        second = expected_v52_config()
        self.assertEqual(
            second["internal_metrics"]["starter"]["season_xwoba"]["weight"],
            0.35,
        )
        self.assertEqual(
            INTERNAL_METRIC_LITERALS["starter"]["season_xwoba"]["weight"],
            0.35,
        )
        with self.assertRaises(TypeError):
            INTERNAL_METRIC_LITERALS["starter"]["season_xwoba"]["weight"] = 0.99
        with self.assertRaisesRegex(ValueError, "frozen value"):
            parse_v52_config(first)
        parse_v52_config(second)

    def test_completeness_uncertainty_and_tier_boundaries(self):
        qualities = {
            "starter": 1,
            "offense": 1,
            "bullpen": 1,
            "environment": 0,
            "defense": 0,
            "schedule": 1,
        }
        self.assertEqual(data_completeness_score(qualities), 8.5)
        self.assertEqual(classify_v52(0.63, 8.5, "Medium", critical=False), "A")
        self.assertEqual(classify_v52(0.68, 8.99, "Medium", critical=False), "A")
        self.assertEqual(classify_v52(0.70, 9.0, "Low", critical=True), "UNRATED")
        self.assertEqual(classify_v52(0.70, 7.99, "High", critical=False), "Toss-up")

    def test_eligibility_dispositions_are_frozen_before_outcomes(self):
        cases = {
            "missing_game_id": ("EXCLUDE", "MISSING_GAME_ID"),
            "invalid_competition": ("EXCLUDE", "INVALID_COMPETITION"),
            "missing_teams": ("EXCLUDE", "MISSING_TEAMS"),
            "missing_start": ("EXCLUDE", "MISSING_START_TIME"),
            "missing_venue": ("EXCLUDE", "MISSING_VENUE"),
            "missing_historical_participants": (
                "EXCLUDE",
                "MISSING_RECONSTRUCTED_PARTICIPANTS",
            ),
            "partial_t45": ("SHADOW_UNRATED", "PARTIAL_T45_PARTICIPANTS"),
            "provenance_mismatch": ("EXCLUDE", "PROVENANCE_MISMATCH"),
            "missing_required_feature": (
                "EXCLUDE",
                "MISSING_REQUIRED_FEATURE_INPUT",
            ),
            "as_of_violation": ("RUN_INVALID", "AS_OF_VIOLATION"),
            "prelock_not_scheduled": ("EXCLUDE", "PRELOCK_NOT_SCHEDULED"),
        }
        for event, expected in cases.items():
            with self.subTest(event=event):
                self.assertEqual(eligibility_disposition(event), expected)

    def test_parser_and_documentation_schema_have_identical_closed_keys(self):
        raw = json.loads(V52_CONFIG.read_text())
        schema = json.loads(V52_SCHEMA.read_text())
        self.assertEqual(set(raw), V52_REQUIRED_KEYS)
        self.assertEqual(set(schema["required"]), V52_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), V52_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        for mutation in (
            lambda value: value.__setitem__("unknown", 1),
            lambda value: value.pop("tier_cascade"),
        ):
            changed = copy.deepcopy(raw)
            mutation(changed)
            with self.assertRaisesRegex(ValueError, "keys"):
                parse_v52_config(changed)

    def test_all_tier_boundaries_and_isolation(self):
        cases = [
            (0.70, 9.0, "Low", False, "S"),
            (0.68, 8.99, "Medium", False, "A"),
            (0.63, 8.5, "Medium", False, "A"),
            (0.58, 8.0, "Medium", False, "B"),
            (0.54, 8.0, "Medium", False, "Lean"),
            (0.539999, 10, "Low", False, "Toss-up"),
            (0.90, 7.999, "High", False, "Toss-up"),
            (0.90, 10, "Low", True, "UNRATED"),
        ]
        for probability, completeness, uncertainty, critical, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_v52(
                        probability, completeness, uncertainty, critical=critical
                    ),
                    expected,
                )
        clipped = {
            "starter": -1,
            "offense": 2,
            "bullpen": 1,
            "environment": 1,
            "defense": 1,
            "schedule": 1,
        }
        self.assertEqual(data_completeness_score(clipped), 6.5)
        key = ranking_key(0.63, 8.5, 4.0, "2025-05-01T17:00:00Z", 1)
        self.assertEqual(key, (-0.63, -8.5, -4.0, "2025-05-01T17:00:00Z", 1))
        self.assertEqual(direction_from_p_home(0.5), (None, 0.5, "NO_DIRECTION_AT_50"))
        self.assertEqual(
            optional_neutral("environment"),
            (50.0, 0.0, "ENV_NEUTRAL_NO_ASOF_SOURCE"),
        )
        self.assertEqual(
            optional_neutral("defense"),
            (50.0, 0.0, "DEF_NEUTRAL_NO_ASOF_SOURCE"),
        )
        qualities = {
            "starter": 0.8,
            "offense": 0.7,
            "bullpen": 0.6,
            "environment": 0,
            "defense": 0,
            "schedule": 1,
        }
        baseline = data_completeness_score(qualities)
        expected_tier = classify_v52(0.63, baseline, "Medium", critical=False)
        expected_rank = ranking_key(0.63, baseline, 4, "2025-05-01T17:00:00Z", 1)
        for agreement, legacy in ((0, 0), (1, 10), (0.37, 4.2)):
            score = data_completeness_score(
                qualities,
                component_agreement=agreement,
                legacy_selection_quality_score=legacy,
            )
            self.assertEqual(score, baseline)
            self.assertEqual(
                classify_v52(0.63, score, "Medium", critical=False), expected_tier
            )
            self.assertEqual(
                ranking_key(0.63, score, 4, "2025-05-01T17:00:00Z", 1),
                expected_rank,
            )
        self.assertEqual(classify_uncertainty(7.49, 200, False, critical=False), "High")
        self.assertEqual(
            classify_uncertainty(8.99, 200, False, critical=False), "Medium"
        )
        self.assertEqual(
            classify_uncertainty(9.0, 149, False, critical=False), "Medium"
        )
        self.assertEqual(classify_uncertainty(9.0, 150, False, critical=False), "Low")
        self.assertEqual(
            read_legacy_confidence({"confidence": 4.2}),
            {"legacy_selection_quality_score": 4.2},
        )

    def test_ranking_timestamp_is_one_canonical_utc_representation(self):
        early = ranking_key(0.63, 8.5, 4, "2025-05-01T07:00:00Z", 2)
        late = ranking_key(0.63, 8.5, 4, "2025-05-01T17:00:00Z", 1)
        self.assertLess(early, late)
        for value in (
            "2025-05-01T17:00:00+00:00",
            "2025-05-01T13:00:00-04:00",
            "2025-05-01T17:00:00",
            "2025-05-01T17:00:00.000Z",
            "2025-5-1T7:00:00Z",
            "2025-02-29T17:00:00Z",
            "not-a-time",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "canonical UTC"):
                    ranking_key(0.63, 8.5, 4, value, 1)

    def test_huge_integer_numeric_inputs_raise_value_error(self):
        huge = 10**10000
        with self.assertRaisesRegex(ValueError, "finite numeric"):
            classify_v52(huge, 8.5, "Medium", critical=False)
        qualities = {
            "starter": huge,
            "offense": 1,
            "bullpen": 1,
            "environment": 1,
            "defense": 1,
            "schedule": 1,
        }
        with self.assertRaisesRegex(ValueError, "finite numeric"):
            data_completeness_score(qualities)

    def test_schema_mirrors_parser_constants_and_frozen_values(self):
        raw = json.loads(V52_CONFIG.read_text())
        schema = json.loads(V52_SCHEMA.read_text())
        self.assertEqual(schema["const"], raw)
        self.assertEqual(set(schema["required"]), V52_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            parse_v52_config(copy.deepcopy(schema["const"])).raw,
            parse_v52_config(raw).raw,
        )

    def test_every_nested_config_container_is_closed_and_frozen(self):
        raw = json.loads(V52_CONFIG.read_text())
        for path, changed in self._container_mutations(raw):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    parse_v52_config(changed)

    def test_change_contract_is_exact_and_hash_bound(self):
        contract = parse_change_contract(json.loads(CHANGE_CONTRACT.read_text()))
        self.assertEqual(contract["unchanged"], CHANGE_UNCHANGED)
        self.assertEqual(contract["changed"], CHANGE_CHANGED)
        self.assertEqual(contract["forbidden_from_2025_diagnostic"], CHANGE_FORBIDDEN)
        self.assertEqual(contract["config_sha256"], sha256_file(V52_CONFIG))
        self.assertEqual(
            contract["approved_design_commit"],
            protocols.CURRENT_APPROVED_DESIGN_COMMIT,
        )
        self.assertEqual(
            contract["approved_design_blob_sha256"],
            protocols.CURRENT_APPROVED_DESIGN_SHA256,
        )
        self.assertEqual(load_change_contract(ROOT), contract)

    def test_contract_loader_rejects_config_and_design_digest_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            load_change_contract(root, approved_design_root=ROOT)

            config = root / "config/v5_2_qd.json"
            config.write_bytes(config.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "config digest"):
                load_change_contract(root, approved_design_root=ROOT)

            write_v52_config_contracts(root, approved_design_root=ROOT)
            config_value = json.loads(config.read_text())
            config_value["component_weights"]["bullpen"] = 0.19
            config.write_bytes(canonical_json_bytes(config_value) + b"\n")
            contract_path = root / "protocols/v5_2_qd/change_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["config_sha256"] = sha256_file(config)
            contract_path.write_bytes(canonical_json_bytes(contract) + b"\n")
            with self.assertRaisesRegex(ValueError, "frozen value"):
                load_change_contract(root, approved_design_root=ROOT)

            write_v52_config_contracts(root, approved_design_root=ROOT)
            contract_path = root / "protocols/v5_2_qd/change_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["approved_design_blob_sha256"] = "0" * 64
            contract_path.write_bytes(canonical_json_bytes(contract) + b"\n")
            with self.assertRaisesRegex(ValueError, "design digest"):
                load_change_contract(root, approved_design_root=ROOT)

    def test_generated_contracts_are_byte_deterministic(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            root_a, root_b = Path(first), Path(second)
            write_v52_config_contracts(root_a, approved_design_root=ROOT)
            write_v52_config_contracts(root_b, approved_design_root=ROOT)
            paths = (
                "config/v5_2_qd.json",
                "schemas/v5_2_qd.schema.json",
                "protocols/v5_2_qd/change_contract.json",
            )
            for relative in paths:
                self.assertEqual(
                    (root_a / relative).read_bytes(), (root_b / relative).read_bytes()
                )

    def test_bundle_resolves_design_before_creating_output_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            bad_design = Path(temporary) / "not-a-repository"
            bad_design.mkdir()
            with self.assertRaisesRegex(ValueError, "approved design blob unavailable"):
                write_v52_config_contracts(root, approved_design_root=bad_design)
            self.assertFalse(root.exists())

    def test_bundle_replace_failure_rolls_back_content_presence_modes_and_temps(self):
        relative_paths = (
            Path("config/v5_2_qd.json"),
            Path("schemas/v5_2_qd.schema.json"),
            Path("protocols/v5_2_qd/change_contract.json"),
        )
        for initial_state in (
            (True, True, True),
            (False, False, False),
            (True, False, True),
        ):
            with (
                self.subTest(initial_state=initial_state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                old = {}
                modes = {}
                for index, (relative, preexisting) in enumerate(
                    zip(relative_paths, initial_state, strict=True)
                ):
                    if preexisting:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        old[path] = f"old-{index}".encode()
                        modes[path] = 0o600 + index
                        path.write_bytes(old[path])
                        path.chmod(modes[path])
                original_replace = protocols._replace_pinned
                final_replacements = 0

                def fail_second_final(*args, **kwargs):
                    nonlocal final_replacements
                    destination = kwargs["destination_path"]
                    if Path(destination) in tuple(
                        root / item for item in relative_paths
                    ):
                        final_replacements += 1
                        if final_replacements == 2:
                            raise OSError("second bundle replace failed")
                    return original_replace(*args, **kwargs)

                with patch(
                    "mlb_backtest.protocols._replace_pinned",
                    side_effect=fail_second_final,
                ):
                    with self.assertRaisesRegex(
                        OSError, "second bundle replace failed"
                    ):
                        write_v52_config_contracts(root, approved_design_root=ROOT)
                for relative, preexisting in zip(
                    relative_paths, initial_state, strict=True
                ):
                    path = root / relative
                    if preexisting:
                        self.assertEqual(path.read_bytes(), old[path])
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[path])
                    else:
                        self.assertFalse(path.exists())
                self.assertEqual(list(root.rglob(".*.tmp")), [])

    def test_bundle_success_preserves_existing_modes_and_uses_0644_for_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = (
                root / "config/v5_2_qd.json",
                root / "schemas/v5_2_qd.schema.json",
                root / "protocols/v5_2_qd/change_contract.json",
            )
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old")
                path.chmod(0o600 + index)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            self.assertEqual(
                tuple(stat.S_IMODE(path.stat().st_mode) for path in paths),
                (0o600, 0o601, 0o602),
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            paths = (
                root / "config/v5_2_qd.json",
                root / "schemas/v5_2_qd.schema.json",
                root / "protocols/v5_2_qd/change_contract.json",
            )
            self.assertEqual(
                tuple(stat.S_IMODE(path.stat().st_mode) for path in paths),
                (0o644, 0o644, 0o644),
            )

    @staticmethod
    def _container_mutations(raw):
        mutations = []

        def visit(value, path):
            if isinstance(value, dict):
                changed = copy.deepcopy(raw)
                target = changed
                for part in path:
                    target = target[part]
                target["__unknown__"] = None
                mutations.append((f"{path}.unknown", changed))
                for key, child in value.items():
                    visit(child, (*path, key))
            elif isinstance(value, list):
                changed = copy.deepcopy(raw)
                target = changed
                for part in path:
                    target = target[part]
                target.append(copy.deepcopy(value[-1]) if value else None)
                mutations.append((f"{path}.append", changed))
                for index, child in enumerate(value):
                    visit(child, (*path, index))

        visit(raw, ())
        return mutations


if __name__ == "__main__":
    unittest.main()
