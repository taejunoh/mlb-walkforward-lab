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
from jsonschema import Draft202012Validator

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
SOURCE_HISTORICAL_SPEC = (
    ROOT
    / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
R4_SOURCE_HISTORICAL_SPEC = (
    ROOT
    / "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json"
)
RUN_SCHEMA = ROOT / "schemas/v5_2_run_spec.schema.json"
PROSPECTIVE_SPEC = ROOT / "protocols/v5_2_qd/prospective_2026.json"
PROSPECTIVE_SCHEMA = ROOT / "schemas/v5_2_prospective.schema.json"


class ProspectiveProtocolTest(unittest.TestCase):
    def setUp(self):
        self._schedule_signer = Ed25519PrivateKey.generate()
        public_key = self._schedule_signer.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        public_key_patch = patch(
            "mlb_backtest.protocols.PROSPECTIVE_SCHEDULE_RECEIPT_PUBLIC_KEY_HEX",
            public_key.hex(),
        )
        public_key_patch.start()
        self.addCleanup(public_key_patch.stop)

    def _trusted_clock(self, value):
        return patch(
            "mlb_backtest.protocols._trusted_utc_now",
            return_value=datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ"),
        )

    def _clock_evidence(
        self,
        *,
        root=None,
        game_pk=777001,
        observed_at="2026-07-13T21:55:00Z",
        offset=0.25,
        uncertainty=0.10,
        source="chrony:time.cloudflare.com",
        evidence_sha256="c" * 64,
        countdown_revision=0,
        basis="2026-07-13T23:10:00Z",
        persisted_at="2026-07-13T22:00:00Z",
    ):
        basis_value = datetime.strptime(basis, "%Y-%m-%dT%H:%M:%SZ")
        window_start = (basis_value - protocols.timedelta(minutes=50)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        lock_at = (basis_value - protocols.timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if root is not None:
            raw_bytes = canonical_json_bytes(
                {
                    "schema_version": 1,
                    "official_date": "2026-07-13",
                    "gamePk": game_pk,
                    "countdown_revision": countdown_revision,
                    "window_start_utc": window_start,
                    "lock_at_utc": lock_at,
                    "sampled_at_utc": observed_at,
                    "offset_seconds": offset,
                    "source": source,
                    "uncertainty_seconds": uncertainty,
                }
            ) + b"\n"
            with self._trusted_clock(persisted_at):
                return protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=game_pk,
                    observed_at_utc=observed_at,
                    offset_seconds=offset,
                    uncertainty_seconds=uncertainty,
                    source=source,
                    raw_bytes=raw_bytes,
                )
        return protocols.ProspectiveClockEvidence(
            official_date="2026-07-13",
            game_pk=game_pk,
            countdown_revision=countdown_revision,
            window_start_utc=window_start,
            lock_at_utc=lock_at,
            sampled_at_utc=observed_at,
            offset_seconds=offset,
            uncertainty_seconds=uncertainty,
            source=source,
            evidence_sha256=evidence_sha256,
            raw_path=f"audit/v5_2_qd/prospective_2026/clock_evidence_raw/2026-07-13/{game_pk}/{evidence_sha256}.bin",
            audit_path=f"audit/v5_2_qd/prospective_2026/clock_evidence/2026-07-13/{game_pk}/{evidence_sha256}.json",
            persisted_at_precise_utc="2026-07-13T22:00:00.000000Z",
            persisted_at_utc="2026-07-13T22:00:00Z",
        )

    def _signed_schedule_material(self, game_pks):
        games = []
        for index, game_pk in enumerate(sorted(game_pks)):
            games.append(
                {
                    "gamePk": game_pk,
                    "gameType": "R",
                    "gameDate": "2026-07-13T23:10:00Z",
                    "officialDate": "2026-07-13",
                    "status": {"detailedState": "Scheduled"},
                    "teams": {
                        "away": {
                            "team": {
                                "id": 1000 + index * 2,
                                "name": f"Away {game_pk}",
                            }
                        },
                        "home": {
                            "team": {
                                "id": 1001 + index * 2,
                                "name": f"Home {game_pk}",
                            }
                        },
                    },
                }
            )
        snapshot = canonical_json_bytes(
            {"dates": [{"date": "2026-07-13", "games": games}]}
        ) + b"\n"
        population_sha = hashlib.sha256(
            canonical_json_bytes(sorted(game_pks))
        ).hexdigest()
        acquisition_receipt = canonical_json_bytes(
            {
                "schema_version": 1,
                "source": "MLB Stats API schedule",
                "official_date": "2026-07-13",
                "query": protocols._prospective_schedule_query("2026-07-13"),
                "observed_at_utc": "2026-07-13T19:59:00Z",
                "response_complete": True,
                "schedule_snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
                "game_count": len(game_pks),
                "population_sha256": population_sha,
            }
        ) + b"\n"
        signature = self._schedule_signer.sign(acquisition_receipt)
        return snapshot, acquisition_receipt, signature

    def _seal_date_roster(self, root, game_pks):
        snapshot, acquisition_receipt, signature = self._signed_schedule_material(
            game_pks
        )
        with self._trusted_clock("2026-07-13T20:00:00Z"):
            return protocols.write_prospective_date_roster(
                root,
                official_date="2026-07-13",
                schedule_source="MLB Stats API schedule",
                schedule_snapshot_bytes=snapshot,
                observed_at_utc="2026-07-13T19:59:00Z",
                acquisition_receipt_bytes=acquisition_receipt,
                acquisition_signature=signature,
            )

    def test_candidate_slate_requires_independent_sealed_date_roster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            candidate = self._disk_candidate(root)
            with (
                self._trusted_clock("2026-07-13T22:26:00Z"),
                self.assertRaisesRegex(ValueError, "date roster.*missing|missing.*date roster"),
            ):
                protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )

    def test_sealed_date_roster_prevents_eligible_ledger_omission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for game_pk in (777001, 777002, 777003):
                self._sealed_snapshot_ledger(root, game_pk=game_pk)
            self._seal_date_roster(root, (777001, 777002, 777003))
            candidates = tuple(
                self._disk_candidate(root, game_pk=game_pk)
                for game_pk in (777001, 777002)
            )
            with (
                self._trusted_clock("2026-07-13T22:26:00Z"),
                self.assertRaisesRegex(ValueError, "roster.*eligible.*omitted|omitted.*777003"),
            ):
                protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=candidates,
                )

    def test_prelock_start_change_restarts_countdown_and_supersedes_old_epoch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            clock_evidence = self._clock_evidence(root=root)
            with self._trusted_clock("2026-07-13T22:22:00Z"):
                old = protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=clock_evidence,
                )
            with self._trusted_clock("2026-07-13T22:30:00Z"):
                protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            revised_clock_evidence = self._clock_evidence(
                root=root,
                countdown_revision=1,
                basis="2026-07-13T23:40:00Z",
                persisted_at="2026-07-13T22:31:00Z",
            )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                new = protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=revised_clock_evidence,
                )
            self.assertEqual(old.snapshot.countdown_revision, 0)
            self.assertEqual(new.snapshot.countdown_revision, 1)
            self.assertEqual(new.snapshot.lock_at_utc, "2026-07-13T22:55:00Z")
            with self._trusted_clock("2026-07-13T22:55:00Z"):
                state = protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self.assertEqual(state.lock_basis_start_utc, "2026-07-13T23:40:00Z")
            self.assertEqual(state.countdown_revision, 1)
            self.assertEqual(state.selected_sequence, 2)

    def test_clock_evidence_is_auditable_and_missing_or_invalid_is_clock_miss(self):
        evidence = self._clock_evidence()
        snapshot = self._snapshot(clock_evidence=evidence)
        self.assertEqual(classify_snapshot(snapshot), "TIMESTAMP_VERIFIED_T45")
        self.assertEqual(
            classify_snapshot(replace(snapshot, clock_evidence=None)),
            "SYSTEM_MISS_CLOCK",
        )
        self.assertEqual(
            classify_snapshot(
                replace(
                    snapshot,
                    clock_evidence=replace(evidence, uncertainty_seconds=-0.01),
                )
            ),
            "SYSTEM_MISS_CLOCK",
        )
        raw = protocols._prospective_snapshot_raw(snapshot)
        self.assertEqual(
            set(raw["clock_evidence"]),
            {
                "official_date",
                "gamePk",
                "countdown_revision",
                "window_start_utc",
                "lock_at_utc",
                "sampled_at_utc",
                "offset_seconds",
                "uncertainty_seconds",
                "source",
                "evidence_sha256",
                "raw_path",
                "audit_path",
                "persisted_at_precise_utc",
                "persisted_at_utc",
            },
        )

    def test_prelock_start_return_to_prior_value_creates_a_new_epoch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            starts = (
                (
                    None,
                    "2026-07-13T21:56:00Z",
                    "2026-07-13T21:57:00Z",
                    "2026-07-13T23:10:00Z",
                ),
                (
                    "2026-07-13T21:58:00Z",
                    "2026-07-13T21:59:00Z",
                    "2026-07-13T22:00:00Z",
                    "2026-07-13T23:40:00Z",
                ),
                (
                    "2026-07-13T22:01:00Z",
                    "2026-07-13T22:02:00Z",
                    "2026-07-13T22:03:00Z",
                    "2026-07-13T23:10:00Z",
                ),
            )
            for revision, (
                transition_at,
                evidence_at,
                receipt_at,
                observed_start,
            ) in enumerate(starts):
                if transition_at is not None:
                    with self._trusted_clock(transition_at):
                        protocols.transition_prospective_snapshot_countdown(
                            root,
                            official_date="2026-07-13",
                            game_pk=777001,
                            observed_scheduled_start_utc=observed_start,
                        )
                evidence = self._clock_evidence(
                    root=root,
                    countdown_revision=revision,
                    basis=observed_start,
                    persisted_at=evidence_at,
                )
                with self._trusted_clock(receipt_at):
                    protocols.record_prospective_snapshot(
                        root,
                        official_date="2026-07-13",
                        game_pk=777001,
                        observed_scheduled_start_utc=observed_start,
                        successful=True,
                        away_starter_present=True,
                        home_starter_present=True,
                        away_lineup_count=9,
                        home_lineup_count=9,
                        clock_evidence=evidence,
                    )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                state = protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            _, receipts = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(
                tuple(receipt.snapshot.countdown_revision for receipt in receipts),
                (0, 1, 2),
            )
            self.assertEqual(state.countdown_revision, 2)
            self.assertIsNone(state.selected_sequence)
            self.assertIsNone(state.lock_basis_start_utc)

    def test_late_prelock_reschedule_is_preserved_as_shadow_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:30:00Z",
                )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                receipt = protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:30:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=None,
                )
            self.assertEqual(
                classify_snapshot(receipt.snapshot), "TIMESTAMP_PARTIAL_T45"
            )
            self.assertEqual(
                receipt.snapshot.reason_codes, ("LATE_RESCHEDULE_UNVERIFIED",)
            )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                state = protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self.assertIsNone(state.selected_sequence)
            self.assertIsNone(state.lock_basis_start_utc)

    def test_clock_raw_bytes_tamper_fails_closed_as_system_miss_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            evidence = self._clock_evidence(root=root)
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
                    clock_evidence=evidence,
                )
            raw_path = root / evidence.raw_path
            raw_path.chmod(0o644)
            raw_path.write_bytes(b"tampered\n")
            self.assertEqual(
                protocols._classify_snapshot_with_root(root, receipt.snapshot),
                "SYSTEM_MISS_CLOCK",
            )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                state = protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self.assertIsNone(state.selected_sequence)

    def test_roster_terminal_coverage_records_clock_miss_without_omission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777002,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777002,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=None,
                )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777002
                )
            self._seal_date_roster(root, (777001, 777002))
            candidate = self._disk_candidate(root, game_pk=777001)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(
                tuple(
                    {
                        "gamePk": row["gamePk"],
                        "disposition": row["disposition"],
                    }
                    for row in slate.game_dispositions
                ),
                (
                    {"gamePk": 777001, "disposition": "OFFICIAL_CANDIDATE"},
                    {"gamePk": 777002, "disposition": "SYSTEM_MISS_CLOCK"},
                ),
            )
            self.assertEqual(slate.date_disposition, "SYSTEM_MISS")
            with self.assertRaisesRegex(ValueError, "SYSTEM_MISS|disposition|Top1"):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            slate_path = root / slate.audit_path
            relabeled = json.loads(slate_path.read_bytes())
            relabeled["date_disposition"] = "LOCK_TOP1"
            slate_path.write_bytes(canonical_json_bytes(relabeled) + b"\n")
            with self.assertRaisesRegex(ValueError, "date disposition"):
                protocols.load_prospective_candidate_slate(
                    root, official_date="2026-07-13"
                )

    def test_mixed_eligible_and_acquisition_miss_blocks_top1(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            self._sealed_snapshot_ledger(
                root,
                game_pk=777002,
                receipts=(("2026-07-13T22:24:00Z", False, False),),
            )
            self._seal_date_roster(root, (777001, 777002))
            candidate = self._disk_candidate(root, game_pk=777001)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(
                tuple(row["disposition"] for row in slate.game_dispositions),
                ("OFFICIAL_CANDIDATE", "SYSTEM_MISS"),
            )
            self.assertEqual(slate.date_disposition, "SYSTEM_MISS")
            with self.assertRaisesRegex(ValueError, "SYSTEM_MISS|disposition|Top1"):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )

    def test_unsigned_caller_subset_cannot_define_authoritative_date_roster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for game_pk in (777001, 777002, 777003):
                self._sealed_snapshot_ledger(root, game_pk=game_pk)
            unsigned_subset = canonical_json_bytes(
                {
                    "official_date": "2026-07-13",
                    "gamePks": [777001, 777002],
                }
            ) + b"\n"
            with (
                self._trusted_clock("2026-07-13T20:00:00Z"),
                self.assertRaisesRegex(ValueError, "signed acquisition|signature"),
            ):
                protocols.write_prospective_date_roster(
                    root,
                    official_date="2026-07-13",
                    schedule_source="MLB Stats API schedule",
                    schedule_snapshot_bytes=unsigned_subset,
                    observed_at_utc="2026-07-13T19:59:00Z",
                )

    def test_schedule_acquisition_trust_root_rejects_every_claim_substitution(self):
        snapshot, receipt, signature = self._signed_schedule_material(
            (777001, 777002, 777003)
        )
        subset_snapshot, _, _ = self._signed_schedule_material((777001, 777002))
        receipt_object = json.loads(receipt)

        def signed_mutation(**updates):
            mutated = {**receipt_object, **updates}
            encoded = canonical_json_bytes(mutated) + b"\n"
            return encoded, self._schedule_signer.sign(encoded)

        wrong_query, wrong_query_signature = signed_mutation(
            query={
                **receipt_object["query"],
                "start": "2026-07-12",
            }
        )
        incomplete, incomplete_signature = signed_mutation(response_complete=False)
        wrong_count, wrong_count_signature = signed_mutation(game_count=2)
        wrong_population, wrong_population_signature = signed_mutation(
            population_sha256="0" * 64
        )
        noncanonical = receipt.rstrip(b"\n") + b" \n"
        cases = (
            ("bad_signature", snapshot, receipt, bytes([signature[0] ^ 1]) + signature[1:]),
            ("raw_subset", subset_snapshot, receipt, signature),
            ("wrong_query", snapshot, wrong_query, wrong_query_signature),
            ("incomplete", snapshot, incomplete, incomplete_signature),
            ("wrong_count", snapshot, wrong_count, wrong_count_signature),
            (
                "wrong_population",
                snapshot,
                wrong_population,
                wrong_population_signature,
            ),
            (
                "noncanonical",
                snapshot,
                noncanonical,
                self._schedule_signer.sign(noncanonical),
            ),
        )
        for label, raw_snapshot, raw_receipt, raw_signature in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                with (
                    self._trusted_clock("2026-07-13T20:00:00Z"),
                    self.assertRaisesRegex(
                        ValueError,
                        "signature|canonical|claims differ",
                    ),
                ):
                    protocols.write_prospective_date_roster(
                        root,
                        official_date="2026-07-13",
                        schedule_snapshot_bytes=raw_snapshot,
                        acquisition_receipt_bytes=raw_receipt,
                        acquisition_signature=raw_signature,
                    )

    def test_schedule_acquisition_receipt_rejects_boolean_integer_claims(self):
        snapshot, receipt, _ = self._signed_schedule_material((777001,))
        invalid = json.loads(receipt)
        invalid["schema_version"] = True
        invalid["game_count"] = True
        invalid_bytes = canonical_json_bytes(invalid) + b"\n"
        signature = self._schedule_signer.sign(invalid_bytes)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                self._trusted_clock("2026-07-13T20:00:00Z"),
                self.assertRaisesRegex(ValueError, "claims differ|integer"),
            ):
                protocols.write_prospective_date_roster(
                    root,
                    official_date="2026-07-13",
                    schedule_snapshot_bytes=snapshot,
                    acquisition_receipt_bytes=invalid_bytes,
                    acquisition_signature=signature,
                )

    def test_date_roster_manifest_cannot_relabel_signed_observation_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            roster = self._seal_date_roster(root, (777001,))
            manifest_path = root / roster.audit_path
            relabeled = json.loads(manifest_path.read_bytes())
            relabeled["observed_at_utc"] = "2026-07-13T19:58:00Z"
            manifest_path.write_bytes(canonical_json_bytes(relabeled) + b"\n")

            with self.assertRaisesRegex(ValueError, "signed acquisition"):
                protocols.load_prospective_date_roster(
                    root, official_date="2026-07-13"
                )

    def test_clock_claims_are_derived_from_raw_and_wrong_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            raw_bytes = canonical_json_bytes(
                {
                    "schema_version": 1,
                    "official_date": "2026-07-13",
                    "gamePk": 777001,
                    "countdown_revision": 99,
                    "window_start_utc": "2026-07-13T22:20:00Z",
                    "lock_at_utc": "2026-07-13T22:25:00Z",
                    "sampled_at_utc": "2026-07-13T21:55:00Z",
                    "source": "chrony:time.cloudflare.com",
                    "offset_seconds": 0.25,
                    "uncertainty_seconds": 0.10,
                }
            ) + b"\n"
            with (
                self._trusted_clock("2026-07-13T22:00:00Z"),
                self.assertRaisesRegex(ValueError, "revision|ledger"),
            ):
                protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_at_utc="2026-07-13T21:55:00Z",
                    offset_seconds=0.25,
                    uncertainty_seconds=0.10,
                    source="chrony:time.cloudflare.com",
                    raw_bytes=raw_bytes,
                )

    def test_clock_raw_schema_rejects_boolean_version_and_parallel_mismatch(self):
        raw = {
            "schema_version": True,
            "official_date": "2026-07-13",
            "gamePk": 777001,
            "countdown_revision": 0,
            "window_start_utc": "2026-07-13T22:20:00Z",
            "lock_at_utc": "2026-07-13T22:25:00Z",
            "sampled_at_utc": "2026-07-13T21:55:00Z",
            "source": "chrony:time.cloudflare.com",
            "offset_seconds": 0.25,
            "uncertainty_seconds": 0.10,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "schema version"):
                protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    raw_bytes=canonical_json_bytes(raw) + b"\n",
                )
            raw["schema_version"] = 1
            with self.assertRaisesRegex(ValueError, "parallel claim"):
                protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    offset_seconds=0.5,
                    raw_bytes=canonical_json_bytes(raw) + b"\n",
                )

    def test_clock_persistence_uses_precise_t50_admission_boundary(self):
        raw_bytes = canonical_json_bytes(
            {
                "schema_version": 1,
                "official_date": "2026-07-13",
                "gamePk": 777001,
                "countdown_revision": 0,
                "window_start_utc": "2026-07-13T22:20:00Z",
                "lock_at_utc": "2026-07-13T22:25:00Z",
                "sampled_at_utc": "2026-07-13T21:55:00Z",
                "source": "chrony:time.cloudflare.com",
                "offset_seconds": 0.25,
                "uncertainty_seconds": 0.10,
            }
        ) + b"\n"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with (
                patch(
                    "mlb_backtest.protocols._trusted_utc_now",
                    return_value=datetime(2026, 7, 13, 22, 20, 0, 1000),
                ),
                self.assertRaisesRegex(ValueError, "T-50|window start"),
            ):
                protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    raw_bytes=raw_bytes,
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
            with patch(
                "mlb_backtest.protocols._trusted_utc_now",
                return_value=datetime(2026, 7, 13, 22, 19, 59, 999000),
            ):
                evidence = protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    raw_bytes=raw_bytes,
                )
            self.assertEqual(evidence.persisted_at_utc, "2026-07-13T22:20:00Z")
            self.assertEqual(
                evidence.persisted_at_precise_utc,
                "2026-07-13T22:19:59.999000Z",
            )
            loaded = protocols.load_prospective_clock_evidence(
                root,
                official_date="2026-07-13",
                game_pk=777001,
                evidence_sha256=evidence.evidence_sha256,
            )
            self.assertEqual(loaded, evidence)
            self.assertTrue(protocols._clock_evidence_structurally_valid(loaded))

            audit_path = root / evidence.audit_path
            delayed = json.loads(audit_path.read_bytes())
            delayed["persisted_at_precise_utc"] = "2026-07-13T22:20:00.001000Z"
            audit_path.write_bytes(canonical_json_bytes(delayed) + b"\n")
            with self.assertRaisesRegex(ValueError, "precise|window"):
                protocols.load_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    evidence_sha256=evidence.evidence_sha256,
                )

    def test_delayed_clock_persistence_is_clock_miss_and_never_official(self):
        raw_bytes = canonical_json_bytes(
            {
                "schema_version": 1,
                "official_date": "2026-07-13",
                "gamePk": 777001,
                "countdown_revision": 0,
                "window_start_utc": "2026-07-13T22:20:00Z",
                "lock_at_utc": "2026-07-13T22:25:00Z",
                "sampled_at_utc": "2026-07-13T21:55:00Z",
                "source": "chrony:time.cloudflare.com",
                "offset_seconds": 0.25,
                "uncertainty_seconds": 0.10,
            }
        ) + b"\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with (
                self._trusted_clock("2026-07-13T22:24:00Z"),
                self.assertRaisesRegex(ValueError, "T-50|window start"),
            ):
                protocols.write_prospective_clock_evidence(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    raw_bytes=raw_bytes,
                )
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
                    clock_evidence=None,
                )
            self.assertEqual(
                protocols._classify_snapshot_with_root(root, receipt.snapshot),
                "SYSTEM_MISS_CLOCK",
            )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self._seal_date_roster(root, (777001,))
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root, official_date="2026-07-13", candidates=()
                )
            self.assertEqual(slate.expected_candidate_count, 0)
            self.assertEqual(
                slate.game_dispositions[0]["disposition"], "SYSTEM_MISS_CLOCK"
            )

    def test_clock_evidence_requires_current_open_ledger_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "ledger|OPEN|open"):
                self._clock_evidence(root=root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with self.assertRaisesRegex(ValueError, "revision|ledger"):
                self._clock_evidence(
                    root=root,
                    countdown_revision=1,
                    basis="2026-07-13T23:40:00Z",
                )
            with self.assertRaisesRegex(ValueError, "window|ledger|start"):
                self._clock_evidence(
                    root=root,
                    countdown_revision=0,
                    basis="2026-07-13T23:40:00Z",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            with self.assertRaisesRegex(ValueError, "closed|OPEN|open"):
                self._clock_evidence(root=root, source="chrony:alternate.example")

    def test_reschedule_full_history_builds_candidate_slate_and_top1(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            old_evidence = self._clock_evidence(root=root)
            with self._trusted_clock("2026-07-13T22:24:00Z"):
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
                    clock_evidence=old_evidence,
                )
            with self._trusted_clock("2026-07-13T22:30:00Z"):
                transitioned = protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            self.assertEqual(transitioned.countdown_revision, 1)
            new_evidence = self._clock_evidence(
                root=root,
                countdown_revision=1,
                basis="2026-07-13T23:40:00Z",
                persisted_at="2026-07-13T22:31:00Z",
            )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=new_evidence,
                )
            with self._trusted_clock("2026-07-13T22:55:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self._seal_date_roster(root, (777001,))
            candidate = self._disk_candidate(root)
            self.assertEqual(
                tuple(
                    receipt.snapshot.countdown_revision
                    for receipt in candidate.snapshot_history
                ),
                (0, 1),
            )
            self.assertEqual(candidate.selected_snapshot.countdown_revision, 1)
            with self._trusted_clock("2026-07-13T22:56:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            top1 = protocols.select_prospective_top1(
                root,
                official_date="2026-07-13",
                candidates=(candidate,),
            )
            self.assertEqual(slate.expected_candidate_count, 1)
            self.assertEqual(top1.game_pk, 777001)
            self.assertEqual(top1.verified_snapshot_sha256, candidate.verified_snapshot_sha256)

    def test_countdown_transition_anchor_failure_rolls_back_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                original = protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with (
                self._trusted_clock("2026-07-13T22:30:00Z"),
                patch(
                    "mlb_backtest.protocols._write_snapshot_state_anchor_exclusive",
                    side_effect=OSError("simulated transition anchor failure"),
                ),
                self.assertRaisesRegex(OSError, "transition anchor failure"),
            ):
                protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            rolled_back, receipts = protocols.load_prospective_snapshot_ledger(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(rolled_back, original)
            self.assertEqual(receipts, ())

            with self._trusted_clock("2026-07-13T22:30:00Z"):
                transitioned = protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            anchors = protocols._load_snapshot_state_anchors(
                root, official_date="2026-07-13", game_pk=777001
            )
            self.assertEqual(transitioned.countdown_revision, 1)
            self.assertEqual(len(anchors), 2)
            self.assertIn("/00000001-", anchors[-1].audit_path)

    def test_terminal_disposition_ignores_superseded_revision_clock_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with self._trusted_clock("2026-07-13T22:24:00Z"):
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
                    clock_evidence=None,
                )
            with self._trusted_clock("2026-07-13T22:30:00Z"):
                protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            evidence = self._clock_evidence(
                root=root,
                game_pk=777001,
                countdown_revision=1,
                basis="2026-07-13T23:40:00Z",
                persisted_at="2026-07-13T22:31:00Z",
            )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=False,
                    away_lineup_count=9,
                    home_lineup_count=8,
                    clock_evidence=evidence,
                )
            with self._trusted_clock("2026-07-13T22:55:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self._sealed_snapshot_ledger(root, game_pk=777002)
            self._seal_date_roster(root, (777001, 777002))
            candidate = self._disk_candidate(root, game_pk=777002)
            with self._trusted_clock("2026-07-13T22:56:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(
                dict(slate.game_dispositions[0])["disposition"],
                "TIMESTAMP_PARTIAL_T45",
            )
            self.assertEqual(slate.date_disposition, "LOCK_TOP1")
            top1 = protocols.select_prospective_top1(
                root,
                official_date="2026-07-13",
                candidates=(candidate,),
            )
            self.assertEqual(top1.game_pk, 777002)

    def test_zero_verified_eligible_date_seals_terminal_empty_slate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(
                root,
                receipts=(("2026-07-13T22:24:00Z", True, False),),
            )
            self._seal_date_roster(root, (777001,))
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(),
                )
            self.assertEqual(slate.expected_candidate_count, 0)
            self.assertEqual(slate.expected_candidate_ids, ())
            self.assertEqual(
                slate.date_disposition,
                "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE",
            )

    def test_current_revision_acquisition_failure_is_terminal_system_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            old_evidence = self._clock_evidence(root=root, game_pk=777001)
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=False,
                    away_lineup_count=9,
                    home_lineup_count=8,
                    clock_evidence=old_evidence,
                )
            with self._trusted_clock("2026-07-13T22:30:00Z"):
                protocols.transition_prospective_snapshot_countdown(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                )
            current_evidence = self._clock_evidence(
                root=root,
                game_pk=777001,
                countdown_revision=1,
                basis="2026-07-13T23:40:00Z",
                persisted_at="2026-07-13T22:31:00Z",
            )
            with self._trusted_clock("2026-07-13T22:53:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=False,
                    away_lineup_count=9,
                    home_lineup_count=8,
                    clock_evidence=current_evidence,
                )
            with self._trusted_clock("2026-07-13T22:54:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    observed_scheduled_start_utc="2026-07-13T23:40:00Z",
                    successful=False,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=current_evidence,
                )
            with self._trusted_clock("2026-07-13T22:55:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777001
                )
            self._sealed_snapshot_ledger(
                root,
                game_pk=777002,
                receipts=(("2026-07-13T22:24:00Z", True, False),),
            )
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777003,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            third_evidence = self._clock_evidence(root=root, game_pk=777003)
            with self._trusted_clock("2026-07-13T22:23:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777003,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=False,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=third_evidence,
                )
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777003,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=False,
                    away_lineup_count=9,
                    home_lineup_count=8,
                    clock_evidence=None,
                )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777003
                )
            self._seal_date_roster(root, (777001, 777002, 777003))

            with self._trusted_clock("2026-07-13T22:56:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(),
                )

            self.assertEqual(
                [row["disposition"] for row in slate.game_dispositions],
                [
                    "SYSTEM_MISS",
                    "TIMESTAMP_PARTIAL_T45",
                    "SYSTEM_MISS_CLOCK",
                ],
            )
            self.assertEqual(slate.expected_candidate_count, 0)
            self.assertEqual(slate.date_disposition, "SYSTEM_MISS")

    def test_candidate_slate_rejects_date_disposition_relabeling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(
                root,
                receipts=(("2026-07-13T22:24:00Z", True, False),),
            )
            self._seal_date_roster(root, (777001,))
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                slate = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(),
                )
            slate_path = root / slate.audit_path
            relabeled = json.loads(slate_path.read_bytes())
            relabeled["date_disposition"] = "NO_VERIFIED_ELIGIBLE_DATE"
            slate_path.write_bytes(canonical_json_bytes(relabeled) + b"\n")
            with self.assertRaisesRegex(ValueError, "date disposition"):
                protocols.load_prospective_candidate_slate(
                    root, official_date="2026-07-13"
                )

    def test_top1_revalidates_noncandidate_terminal_ledger_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root, game_pk=777001)
            with self._trusted_clock("2026-07-13T21:00:00Z"):
                protocols.initialize_prospective_snapshot_ledger(
                    root,
                    official_date="2026-07-13",
                    game_pk=777002,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                )
            with self._trusted_clock("2026-07-13T22:24:00Z"):
                protocols.record_prospective_snapshot(
                    root,
                    official_date="2026-07-13",
                    game_pk=777002,
                    observed_scheduled_start_utc="2026-07-13T23:10:00Z",
                    successful=True,
                    away_starter_present=True,
                    home_starter_present=True,
                    away_lineup_count=9,
                    home_lineup_count=9,
                    clock_evidence=None,
                )
            with self._trusted_clock("2026-07-13T22:25:00Z"):
                noncandidate = protocols.seal_prospective_snapshot_ledger(
                    root, official_date="2026-07-13", game_pk=777002
                )
            self._seal_date_roster(root, (777001, 777002))
            candidate = self._disk_candidate(root, game_pk=777001)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            (root / noncandidate.audit_path).unlink()
            with self.assertRaisesRegex(
                ValueError, "terminal ledger coverage|roster.*binding|missing"
            ):
                protocols.select_prospective_top1(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )

    def test_postlock_initialization_requires_exact_closed_snapshot_basis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "closed snapshot ledger|missing"):
                protocols.initialize_postlock_chain(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            with self.assertRaisesRegex(ValueError, "sealed.*basis|basis.*differs"):
                protocols.initialize_postlock_chain(
                    root,
                    official_date="2026-07-13",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:40:00Z",
                    persisted_at_utc="2026-07-13T22:55:00Z",
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
        clock_evidence = self._clock_evidence(root=root, game_pk=game_pk)
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
                    clock_evidence=clock_evidence,
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

    def _initialize_postlock_chain(self, root, **kwargs):
        game_pk = kwargs.get("game_pk", 777001)
        official_date = kwargs.pop("official_date", "2026-07-13")
        try:
            protocols.load_prospective_snapshot_ledger(
                root, official_date=official_date, game_pk=game_pk
            )
        except ValueError:
            self._sealed_snapshot_ledger(
                root,
                game_pk=game_pk,
                official_date=official_date,
                basis=kwargs.get(
                    "lock_basis_start_utc", "2026-07-13T23:10:00Z"
                ),
            )
        return protocols.initialize_postlock_chain(
            root, official_date=official_date, **kwargs
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

    def test_snapshot_ledger_selects_latest_receipt_time_then_sequence(self):
        cases = (
            (
                (
                    ("2026-07-13T22:24:00Z", True, True),
                    ("2026-07-13T22:23:00Z", True, True),
                ),
                1,
            ),
            (
                (
                    ("2026-07-13T22:24:00Z", True, True),
                    ("2026-07-13T22:24:00Z", True, True),
                ),
                2,
            ),
        )
        for receipts, expected_sequence in cases:
            with self.subTest(expected_sequence=expected_sequence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                state = self._sealed_snapshot_ledger(root, receipts=receipts)
                self.assertEqual(state.selected_sequence, expected_sequence)
                candidate = self._disk_candidate(root)
                self.assertEqual(candidate.selected_snapshot.sequence, expected_sequence)
                self.assertEqual(
                    candidate.snapshot_ledger_state["selected_sequence"],
                    expected_sequence,
                )

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
                        clock_evidence=None,
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

    def test_candidate_gate_retains_nonqualifying_early_poll_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(
                root,
                receipts=(
                    ("2026-07-13T21:40:00Z", True, False),
                    ("2026-07-13T22:24:00Z", True, True),
                ),
            )
            candidate = self._disk_candidate(root)
            self.assertEqual(len(candidate.snapshot_history), 2)
            self.assertEqual(candidate.selected_snapshot.sequence, 2)
            self.assertTrue(eligible_for_prospective_official_pool(candidate))

    def test_candidate_gate_rejects_history_and_duplicated_ledger_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(
                root,
                receipts=(
                    ("2026-07-13T22:23:00Z", True, True),
                    ("2026-07-13T22:24:00Z", True, True),
                ),
            )
            candidate = self._disk_candidate(root)
            history = candidate.snapshot_history
            snapshots = candidate.snapshot_candidates
            self.assertEqual(len(history), 2)
            self.assertTrue(eligible_for_prospective_official_pool(candidate))

            rolled_back_state = dict(candidate.snapshot_ledger_state)
            rolled_back_state["snapshot_count"] = 1
            rolled_back_state["head_hash"] = history[0].receipt_hash
            rolled_back_digest = hashlib.sha256(
                canonical_json_bytes(rolled_back_state)
            ).hexdigest()
            mutations = (
                (replace(candidate, snapshot_history=()), "history"),
                (replace(candidate, snapshot_history=history[1:]), "history|sequence"),
                (replace(candidate, snapshot_history=history[:-1]), "history|candidate"),
                (
                    replace(
                        candidate,
                        snapshot_history=history[:-1],
                        snapshot_candidates=snapshots[:-1],
                    ),
                    "count|head|rollback",
                ),
                (
                    replace(candidate, snapshot_history=(history[1], history[0])),
                    "history|sequence|chain",
                ),
                (
                    replace(candidate, snapshot_history=(*history, history[-1])),
                    "history|sequence|chain",
                ),
                (
                    replace(candidate, snapshot_candidates=snapshots[1:]),
                    "history|candidate",
                ),
                (replace(candidate, snapshot_ledger_head="f" * 64), "head"),
                (replace(candidate, snapshot_ledger_count=999), "count"),
                (replace(candidate, snapshot_ledger_status="OPEN"), "status"),
                (replace(candidate, snapshot_ledger_path="attacker.json"), "path"),
                (replace(candidate, snapshot_ledger_digest="f" * 64), "digest"),
                (
                    replace(
                        candidate,
                        snapshot_ledger_state=deep_freeze(rolled_back_state),
                        snapshot_ledger_digest=rolled_back_digest,
                    ),
                    "rollback|count|head|history",
                ),
            )
            for changed, message in mutations:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        eligible_for_prospective_official_pool(changed)

    def test_official_slate_reloads_sealed_ledgers_and_rejects_fabrication_or_rollback(self):
        fabricated = self._candidate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._seal_date_roster(root, (777001,))
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
                    clock_evidence=None,
                )

            with self._trusted_clock("2026-07-13T22:24:00Z"):
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
                    clock_evidence=None,
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
                    clock_evidence=None,
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
                "clock_evidence": None,
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
                    clock_evidence=None,
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
                    clock_evidence=None,
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
            "fd04e8b",
        )
        self.assertEqual(
            getattr(protocols, "CURRENT_APPROVED_DESIGN_SHA256", None),
            "2439a5b355f0257c584abfb18ab6c714a0b24e543e5acdee97f2739e74cb8d9a",
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
        clock_evidence="default",
    ):
        if clock_evidence == "default":
            clock_evidence = self._clock_evidence(offset=offset)
        return protocols.ProspectiveSnapshot(
            game_pk=game_pk,
            lock_basis_start_utc=basis,
            observed_scheduled_start_utc=scheduled,
            lock_at_utc=lock,
            receipt_at_utc=receipt,
            successful=successful,
            complete_participants=complete,
            sequence=sequence,
            countdown_revision=0,
            clock_evidence=clock_evidence,
            reason_codes=(),
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
        snapshot_sequence=1,
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
            "sequence": verified_snapshot.sequence,
            "countdown_revision": verified_snapshot.countdown_revision,
            "clock_evidence": protocols._clock_evidence_raw(
                verified_snapshot.clock_evidence
            ),
            "reason_codes": [],
        }
        verified_snapshot_sha256 = hashlib.sha256(
            canonical_json_bytes(snapshot_raw)
        ).hexdigest()
        verified_snapshot_id = (
            f"snapshot:{game_pk}:{snapshot_sequence}:{verified_snapshot_sha256}"
        )
        candidate_snapshots = (
            tuple(snapshot_candidates)
            if snapshot_candidates is not None
            else (verified_snapshot,)
        )
        snapshot_history = []
        prior_receipt_hash = protocols.SNAPSHOT_LEDGER_GENESIS_HASH
        for snapshot in candidate_snapshots:
            snapshot_sha = hashlib.sha256(
                canonical_json_bytes(protocols._prospective_snapshot_raw(snapshot))
            ).hexdigest()
            unsigned_receipt = protocols.ProspectiveSnapshotReceipt(
                official_date=official_date,
                game_pk=game_pk,
                sequence=snapshot.sequence,
                prior_receipt_hash=prior_receipt_hash,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha,
                persisted_at_utc=snapshot.receipt_at_utc,
                receipt_hash="",
                audit_path="",
            )
            receipt_hash = protocols._snapshot_receipt_hash(unsigned_receipt)
            receipt = replace(
                unsigned_receipt,
                receipt_hash=receipt_hash,
                audit_path=protocols._snapshot_receipt_relative_path(
                    official_date,
                    game_pk,
                    snapshot.sequence,
                    receipt_hash,
                ).as_posix(),
            )
            snapshot_history.append(receipt)
            prior_receipt_hash = receipt_hash
        ledger_state = {
            "official_date": official_date,
            "gamePk": game_pk,
            "current_observed_start_utc": lock_basis,
            "countdown_revision": verified_snapshot.countdown_revision,
            "lock_basis_start_utc": lock_basis,
            "lock_at_utc": lock_receipt,
            "status": "CLOSED",
            "snapshot_count": len(candidate_snapshots),
            "head_hash": prior_receipt_hash,
            "selected_sequence": snapshot_sequence,
            "selected_snapshot_id": verified_snapshot_id,
            "selected_snapshot_sha256": verified_snapshot_sha256,
            "closed_at_utc": lock_receipt,
            "audit_path": (
                "audit/v5_2_qd/prospective_2026/snapshot_ledgers/"
                f"{official_date}/{game_pk}/state.json"
            ),
            "receipt_directory": (
                "audit/v5_2_qd/prospective_2026/snapshot_receipts/"
                f"{official_date}/{game_pk}"
            ),
        }
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
            snapshot_candidates=candidate_snapshots,
            selected_snapshot=verified_snapshot,
            snapshot_history=tuple(snapshot_history),
            snapshot_ledger_status="CLOSED",
            snapshot_ledger_head=prior_receipt_hash,
            snapshot_ledger_count=len(candidate_snapshots),
            snapshot_ledger_path=ledger_state["audit_path"],
            snapshot_ledger_digest=hashlib.sha256(
                canonical_json_bytes(ledger_state)
            ).hexdigest(),
            snapshot_ledger_state=deep_freeze(ledger_state),
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
        self.assertEqual(
            ledger["selected_snapshot_order"],
            ("receipt_at_utc_desc", "sequence_desc"),
        )
        official_pool = spec.raw["official_pool"]
        self.assertEqual(
            official_pool["slate_retry_comparison"],
            "stable_semantic_projection_excluding_persisted_at_utc",
        )
        self.assertEqual(
            official_pool["identical_retry_result"],
            "return_existing_original_bytes_and_persisted_at_utc",
        )
        candidate_fields = official_pool["candidate_record_fields"]
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
                    sequence=1,
                    countdown_revision=0,
                    clock_evidence=self._clock_evidence(
                        observed_at=receipt,
                    ),
                    reason_codes=(),
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

    def test_candidate_slate_date_disposition_uses_frozen_precedence(self):
        def disposition(value):
            return deep_freeze({"gamePk": 777001, "disposition": value})

        cases = (
            (("OFFICIAL_CANDIDATE",), "LOCK_TOP1"),
            (
                ("OFFICIAL_CANDIDATE", "TIMESTAMP_PARTIAL_T45"),
                "LOCK_TOP1",
            ),
            (
                ("OFFICIAL_CANDIDATE", "SYSTEM_MISS_CLOCK"),
                "SYSTEM_MISS",
            ),
            (("OFFICIAL_CANDIDATE", "SYSTEM_MISS"), "SYSTEM_MISS"),
            (("TIMESTAMP_PARTIAL_T45",), "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE"),
            (("NO_VERIFIED_T45_SNAPSHOT",), "NO_VERIFIED_ELIGIBLE_DATE"),
            (("RUN_INVALID", "OFFICIAL_CANDIDATE"), "RUN_INVALID"),
        )
        for terminal_names, expected in cases:
            with self.subTest(terminal_names=terminal_names):
                self.assertEqual(
                    protocols._candidate_slate_date_disposition(
                        tuple(disposition(value) for value in terminal_names)
                    ),
                    expected,
                )

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
                "history|candidate|latest|selected",
            ),
            (
                replace(candidate, recorded_at_utc="2026-07-13T22:23:59Z"),
                "recorded_at|receipt",
            ),
            (
                replace(candidate, official_date="2026-07-14"),
                "official_date|basis|history|identity",
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
            self._seal_date_roster(root, (777001,))
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
            self._seal_date_roster(root, (777001, 777002))
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
            self._seal_date_roster(root, (777001,))
            for candidates, message in (
                ((candidate, other_date), "one official date"),
                ((), "roster.*eligible.*omitted|omitted"),
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
            self._seal_date_roster(root, (777001, 777002))
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
            self._seal_date_roster(root, (777001,))
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

    def test_candidate_slate_identical_retry_keeps_original_persistence_and_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._sealed_snapshot_ledger(root)
            self._seal_date_roster(root, (777001,))
            candidate = self._disk_candidate(root)
            with self._trusted_clock("2026-07-13T22:26:00Z"):
                first = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            path = root / first.audit_path
            first_bytes = path.read_bytes()
            with self._trusted_clock("2026-07-13T22:27:00Z"):
                second = protocols.write_prospective_candidate_slate(
                    root,
                    official_date="2026-07-13",
                    candidates=(candidate,),
                )
            self.assertEqual(second, first)
            self.assertEqual(second.persisted_at_utc, "2026-07-13T22:26:00Z")
            self.assertEqual(path.read_bytes(), first_bytes)

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
            self._seal_date_roster(root, (777001, 777002))
            first = self._disk_candidate(root, game_pk=777001)
            second = self._disk_candidate(root, game_pk=777002)
            results = race(
                root,
                ((first, second), (replace(first, p_home=0.64), second)),
            )
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
            self._seal_date_roster(root, (777001,))
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
            state = self._initialize_postlock_chain(
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
            initial = self._initialize_postlock_chain(
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
                self._initialize_postlock_chain(
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
            state = self._initialize_postlock_chain(
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
            old_state = self._initialize_postlock_chain(
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
                state = self._initialize_postlock_chain(
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
                    self._initialize_postlock_chain(
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
            state = self._initialize_postlock_chain(
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
                        prior_state = self._initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                    else:
                        prior_state = None
                        self._sealed_snapshot_ledger(root)
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
                            self._initialize_postlock_chain(
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
                        retried = self._initialize_postlock_chain(
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
                        prior_state = self._initialize_postlock_chain(
                            root,
                            game_pk=777001,
                            lock_basis_start_utc="2026-07-13T23:10:00Z",
                            persisted_at_utc="2026-07-13T22:25:00Z",
                        )
                    else:
                        prior_state = None
                        self._sealed_snapshot_ledger(root)
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
                            self._initialize_postlock_chain(
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
                    prior_state = self._initialize_postlock_chain(
                        root,
                        game_pk=777001,
                        lock_basis_start_utc="2026-07-13T23:10:00Z",
                        persisted_at_utc="2026-07-13T22:25:00Z",
                    )
                else:
                    prior_state = None
                    self._sealed_snapshot_ledger(root)
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
                        self._initialize_postlock_chain(
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
                    retried = self._initialize_postlock_chain(
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
                self._initialize_postlock_chain(
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
            state = self._initialize_postlock_chain(
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
            self._initialize_postlock_chain(
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
                self._initialize_postlock_chain(
                    alias / "workspace",
                    game_pk=777001,
                    lock_basis_start_utc="2026-07-13T23:10:00Z",
                    persisted_at_utc="2026-07-13T22:25:00Z",
                )

    def test_persisted_chain_rejects_aliases_and_preserves_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = self._initialize_postlock_chain(
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
            "31386a811338ed8d7497b6c10f481b8b2be9735a2cd86576c9c6b4d4b942c622",
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
            pool["final_lock_receipt_at_utc"], "max_roster_terminal_lock_at_utc"
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
    @staticmethod
    def _old_schema_branch(schema):
        return schema["oneOf"][0] if "oneOf" in schema else schema

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
        document = json.loads(RUN_SCHEMA.read_text())
        schema = self._old_schema_branch(document)
        self.assertEqual(set(schema["required"]), RUN_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), RUN_REQUIRED_KEYS)
        fold_schema = document.get("$defs", schema.get("$defs"))["fold"]
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
        schema = self._old_schema_branch(json.loads(RUN_SCHEMA.read_text()))
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

    def test_source_run_spec_is_a_closed_source_only_authority_not_old_run_spec(self):
        self.assertTrue(
            hasattr(protocols, "expected_superseding_source_run"),
            "separate SourceRunSpec API is missing",
        )
        expected = protocols.expected_superseding_source_run()
        self.assertEqual(set(expected), protocols.SOURCE_RUN_REQUIRED_KEYS)
        source = protocols.parse_source_run_spec(expected)
        self.assertIsInstance(source, protocols.SourceRunSpec)
        self.assertEqual(source.run_id, "historical_walkforward_500_superseding_565534")
        self.assertEqual(source.authority_scope, "SOURCE_ONLY_THROUGH_SOURCES_READY")
        self.assertEqual(source.required_source_years, tuple(range(2018, 2025)))
        self.assertEqual(source.forbidden_source_years, (2025,))
        forbidden_fragments = (
            "fold",
            "target_count",
            "selection",
            "model_card",
            "features",
            "locked",
            "outcomes",
            "oof",
            "reports_500",
        )
        self.assertFalse(
            any(fragment in key for key in source.raw for fragment in forbidden_fragments)
        )
        with self.assertRaises(ValueError):
            parse_run_spec(expected)

    def test_superseding_565534_run_spec_is_source_only_and_namespace_disjoint(self):
        self.assertTrue(SOURCE_HISTORICAL_SPEC.is_file())
        source = protocols.load_source_run_spec(SOURCE_HISTORICAL_SPEC)
        old = load_run_spec(HISTORICAL_SPEC)
        self.assertEqual(source.raw, deep_freeze(protocols.expected_superseding_source_run()))
        self.assertEqual(
            set(source.raw["namespaces"]), {"raw", "sources", "audit", "reports"}
        )
        self.assertTrue(all(value is False for value in source.downstream_authority.values()))
        old_roots = tuple(str(value) for value in old.raw["namespaces"].values())
        new_roots = tuple(str(value) for value in source.raw["namespaces"].values())
        self.assertTrue(
            all(
                not (left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/"))
                for left in old_roots
                for right in new_roots
            )
        )

    def test_combined_run_schema_writer_preserves_all_exact_branches(self):
        schema = protocols.historical_authority_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(len(schema["oneOf"]), 5)
        old_raw = expected_historical_run()
        source_raw = protocols.expected_superseding_source_run()
        recovery_raw = protocols.expected_recovery_source_run()
        r3_raw = protocols.expected_r3_recovery_source_run()
        r4_raw = protocols.expected_r4_recovery_source_run()
        self.assertEqual(schema["oneOf"][0]["const"], old_raw)
        self.assertEqual(schema["oneOf"][1]["const"], source_raw)
        self.assertEqual(schema["oneOf"][2]["const"], recovery_raw)
        self.assertEqual(schema["oneOf"][3]["const"], r3_raw)
        self.assertEqual(schema["oneOf"][4]["const"], r4_raw)
        for raw, branch in zip(
            (old_raw, source_raw, recovery_raw, r3_raw, r4_raw),
            schema["oneOf"],
            strict=True,
        ):
            self.assertEqual(set(branch["required"]), set(raw))
            self.assertEqual(set(branch["properties"]), set(raw))
            self.assertFalse(branch["additionalProperties"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "protocols/v5_2_qd/historical_walkforward_500.json"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(b"sealed-old-authority\n")
            protocols.write_superseding_source_authority(root)
            self.assertEqual(
                (root / "schemas/v5_2_run_spec.schema.json").read_bytes(),
                canonical_json_bytes(schema) + b"\n",
            )
            self.assertEqual(
                json.loads(
                    (
                        root
                        / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
                    ).read_bytes()
                ),
                source_raw,
            )
            self.assertEqual(old_path.read_bytes(), b"sealed-old-authority\n")
            source_path = (
                root
                / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
            )
            sealed_source = source_path.read_bytes()
            write_historical_contract(root)
            self.assertEqual(source_path.read_bytes(), sealed_source)

    def test_r4_recovery_source_run_is_exact_closed_r3_substitution(self):
        r3 = protocols.expected_r3_recovery_source_run()
        r4 = protocols.expected_r4_recovery_source_run()
        self.assertEqual(
            r4["run_id"],
            "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4",
        )
        self.assertEqual(
            r4["namespaces"]["raw"],
            "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw",
        )
        self.assertEqual(
            r4["namespaces"]["sources"],
            "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources",
        )
        self.assertEqual(
            r4["namespaces"]["audit"],
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4",
        )
        self.assertEqual(
            r4["namespaces"]["reports"],
            "reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4",
        )
        self.assertEqual(r4["label"], r3["label"])
        self.assertEqual(r4["parser_policy"], r3["parser_policy"])
        for key in set(r3) - {"run_id", "namespaces"}:
            self.assertEqual(r4[key], r3[key], key)

    def test_r4_recovery_source_run_literal_file_and_parser_are_exact(self):
        expected = protocols.expected_r4_recovery_source_run()
        self.assertTrue(R4_SOURCE_HISTORICAL_SPEC.is_file())
        literal = json.loads(R4_SOURCE_HISTORICAL_SPEC.read_text())
        self.assertEqual(literal, expected)
        self.assertEqual(
            R4_SOURCE_HISTORICAL_SPEC.read_bytes(),
            canonical_json_bytes(expected) + b"\n",
        )
        parsed = protocols.load_source_run_spec(R4_SOURCE_HISTORICAL_SPEC)
        self.assertEqual(parsed.raw, deep_freeze(expected))
        self.assertEqual(parsed.run_id, expected["run_id"])

    def test_r4_recovery_source_run_rejects_mutations_near_matches_and_extra_keys(self):
        expected = protocols.expected_r4_recovery_source_run()
        mutations = (
            ("run_id", "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4x"),
            ("raw namespace", "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4x/raw"),
        )
        for label, value in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(expected)
                if label == "run_id":
                    changed["run_id"] = value
                else:
                    changed["namespaces"]["raw"] = value
                with self.assertRaises(ValueError):
                    protocols.parse_source_run_spec(changed)
        changed = copy.deepcopy(expected)
        changed["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "keys"):
            protocols.parse_source_run_spec(changed)
        changed = copy.deepcopy(expected)
        changed["run_id"] = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
        with self.assertRaises(ValueError):
            protocols.parse_source_run_spec(changed)

    def test_r3_recovery_source_run_remains_byte_stable_and_accepted(self):
        r3_path = ROOT / "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
        before = r3_path.read_bytes()
        parsed = protocols.load_source_run_spec(r3_path)
        self.assertEqual(parsed.raw, deep_freeze(protocols.expected_r3_recovery_source_run()))
        self.assertEqual(r3_path.read_bytes(), before)

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


class ProtectedExecutionSchemaTest(unittest.TestCase):
    """Draft 2020-12 boundary tests for the Swift protected-evidence contracts."""

    @staticmethod
    def _hash(char="a"):
        return char * 64

    def _fixtures(self):
        h = self._hash
        ns = "org.mlb-walkforward.r4.protected-execution-authority/v1"
        aid = "r4-disjoint-custody-v2-execution-v1"
        key_a = "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1"
        checkout_entries = [
            {"path": "checkout", "type": "directory", "mode": "0555", "size": 0, "sha256": h("a")},
            {"path": "checkout/tool", "type": "regular", "mode": "0444", "size": 1, "sha256": h("b")},
        ]
        runtime_entries = [
            {"path": "runtime", "type": "directory", "mode": "0555", "size": 0, "sha256": h("c")},
            {"path": "runtime/closure", "type": "directory", "mode": "0555", "size": 0, "sha256": h("d")},
            {"path": "runtime/closure/python311", "type": "directory", "mode": "0555", "size": 0, "sha256": h("e")},
            {"path": "runtime/closure/python311/bin", "type": "directory", "mode": "0555", "size": 0, "sha256": h("f")},
            {"path": "runtime/closure/python311/bin/python3.11", "type": "regular", "mode": "0555", "size": 1, "sha256": h("0")},
        ]
        environment = ["HOME=__R4_STAGE__/home", "LANG=C", "LC_ALL=C", "PATH=/usr/bin:/bin", "PIP_CONFIG_FILE=/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK=1", "PIP_NO_INPUT=1", "PYTHONNOUSERSITE=1", "TMPDIR=__R4_STAGE__/tmp"]
        wheel = {"path": "artifacts/wheel.whl", "kind": "wheel", "filename": "wheel.whl", "version": "1.0", "url": "https://example.invalid/wheel.whl", "sha256": h("2"), "size": 2}
        artifacts = [{"path": "artifacts/python.tar.gz", "kind": "python_archive", "filename": "python.tar.gz", "version": "3.11.15", "url": "https://example.invalid/python.tar.gz", "sha256": h("1"), "size": 3}, wheel]
        helpers = [{"path": "bootstrap/provisioner", "sha256": h("3")}, {"path": "bootstrap/provisioner.codesign-requirement.txt", "sha256": h("4")}]
        sandbox_profile = "(version 1)\n(deny default)\n(deny network*)\n(allow file-read* (subpath \"__R4_STAGE__/runtime/closure/python311\") (subpath \"__R4_STAGE__/wheelhouse\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\") (literal \"/dev/null\") (subpath \"/System\") (subpath \"/usr/lib\"))\n(allow file-write* (subpath \"__R4_STAGE__/runtime\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\"))"
        codesign_launcher = {"identifier": "com.apple.sandbox-exec", "designated_requirement": "identifier \"com.apple.sandbox-exec\" and anchor apple", "team_identifier": None, "certificate_chain_sha256": h("5"), "sha256": h("6")}
        codesign_interpreter = {"identifier": "python3.11", "designated_requirement": "identifier python3.11", "team_identifier": None, "certificate_chain_sha256": h("7"), "sha256": h("8")}
        sandbox_launcher = {"path": "/usr/bin/sandbox-exec", "descriptor_identity_sha256": h("9"), "sha256": h("a"), "codesign_identity": codesign_launcher}
        base_interpreter = {"path": "runtime/closure/python311/bin/python3.11", "sha256": h("b"), "mach_o_sha256": h("c"), "codesign_identity": codesign_interpreter}
        import_probe = 'import importlib,json,sys\nmods=("cryptography","jsonschema","numpy","scipy")\nprint(json.dumps({"version":sys.version.split()[0],"paths":sys.path,"files":[importlib.import_module(name).__file__ for name in mods]},sort_keys=True,separators=(",",":")))'
        child_execution = {"cwd": "runtime", "environment": environment, "pip_argv": ["-I", "-s", "-E", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check", "__R4_STAGE__/wheelhouse/wheel.whl"], "import_argv": ["-I", "-s", "-E", "-c", import_probe], "timeout_seconds": 300, "max_output_bytes": 1000000, "sandbox_profile_utf8": sandbox_profile, "sandbox_profile_template_sha256": h("d"), "child_command_template_sha256": h("e"), "read_allow": ["runtime/closure/python311", "wheelhouse", "home", "tmp", "/dev/null", "/System", "/usr/lib"], "write_allow": ["runtime", "home", "tmp"], "network_denied": True}
        staged_macho = [{"path": "runtime/closure/python311/bin/python3.11", "descriptor_identity_sha256": h("a"), "sha256": h("b"), "codesign_identity_sha256": h("c"), "mach_o_observation": {"path": "runtime/closure/python311/bin/python3.11", "slices": [{"cpu_type": 1, "cpu_subtype": 2, "file_type": 2, "uuid": "0123456789abcdef0123456789abcdef", "code_signature_offset": 64, "code_signature_size": 128}], "dependencies": []}, "dependency_closure_sha256": h("d")}]
        return {
            "r4_runtime_input_bundle_v2.schema.json": {"kind": "R4_RUNTIME_INPUT_BUNDLE_V2", "namespace": ns, "authority_id": aid, "lock_path": "r4_protected_python311.lock.json", "lock_sha256": h(), "materialization_policy_path": "materialization-policy.json", "git": {"head": "a" * 40, "tree": "b" * 40, "required_blobs": []}, "artifacts": artifacts, "checkout_entries": checkout_entries, "materialization_policy_sha256": h(), "sha256": h()},
            "r4_privileged_materialization_policy_v2.schema.json": {"kind": "R4_PRIVILEGED_MATERIALIZATION_POLICY_V2", "namespace": ns, "authority_id": aid, "python_version": "3.11.15", "archive": {"archive_root": "python", "final_runtime_path": "runtime/closure/python311", "entrypoint": "runtime/closure/python311/bin/python3.11", "max_members": 100000, "max_unpacked_bytes": 8589934592}, "wheel_filenames": [wheel["filename"]], "sandbox_launcher": sandbox_launcher, "base_interpreter": base_interpreter, "child_execution": child_execution, "allowed_loader_roots": ["runtime/closure/python311", "/System/", "/usr/lib/"], "import_modules": ["cryptography", "jsonschema", "numpy", "scipy"], "import_probe_sha256": h(), "schema_versions": ["R4_RUNTIME_INPUT_BUNDLE_V2", "R4_PRIVILEGED_MATERIALIZATION_POLICY_V2", "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2", "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2", "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2", "R4_HERMETIC_PYTHON311_RUNTIME_V2", "R4_PROTECTED_EXECUTION_MANIFEST_V2", "R4_PROTECTED_EXECUTION_RECEIPT_V2"], "sha256": h()},
            "r4_privileged_materialization_admission_v2.schema.json": {"kind": "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2", "namespace": ns, "authority_id": aid, "attempt_uuid": "c" * 32, "task14_approval_sha256": h(), "input_bundle_manifest_sha256": h(), "materialization_policy_sha256": h(), "storage_volume_uuid": "123e4567-e89b-12d3-a456-426614174000", "sha256": h()},
            "r4_privileged_materialization_admission_locator_v2.schema.json": {"kind": "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2", "namespace": ns, "authority_id": aid, "attempt_uuid": "c" * 32, "admission_record_sha256": h(), "registry_relative_path": "control/admissions/" + "c" * 32 + ".json", "locator_sha256": h("b"), "sha256": h()},
            "r4_privileged_materialization_journal_v2.schema.json": {"kind": "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2", "namespace": ns, "authority_id": aid, "attempt_uuid": "c" * 32, "admission_record_sha256": h(), "phase": "PUBLISHED", "sequence": 4, "published_closure_sha256": h("b"), "receipt_file_sha256": h("c"), "published_device": 0, "published_inode": 0, "journal_sha256": h("d"), "sha256": h()},
            "r4_hermetic_python311_runtime.schema.json": {"kind": "R4_HERMETIC_PYTHON311_RUNTIME_V2", "namespace": ns, "authority_id": aid, "python_version": "3.11.15", "entrypoint": "runtime/closure/python311/bin/python3.11", "lock_sha256": h(), "materialization_input_sha256": h(), "materialization_policy_sha256": h(), "wheel_set_sha256": h(), "sandbox_launcher_path": "/usr/bin/sandbox-exec", "sandbox_launcher_descriptor_identity_sha256": h("9"), "sandbox_launcher_sha256": h("a"), "sandbox_launcher_codesign_identity_sha256": h("6"), "sandbox_profile_template_sha256": h("d"), "sandbox_profile_sha256": h("e"), "child_command_template_sha256": h("f"), "installer_command_sha256": h("0"), "import_command_sha256": h("1"), "interpreter_pre_install_sha256": h("b"), "interpreter_pre_install_mach_o_sha256": h("c"), "interpreter_pre_install_codesign_identity_sha256": h("8"), "interpreter_post_install_sha256": h("b"), "interpreter_post_install_mach_o_sha256": h("c"), "interpreter_post_install_codesign_identity_sha256": h("8"), "import_probe_sha256": h(), "environment_sha256": h("2"), "working_directory": "runtime", "entries": runtime_entries, "entries_sha256": h(), "runtime_output_closure_sha256": h(), "mach_o_dependencies": [], "dependency_closure_sha256": h(), "pre_install_staged_mach_o_observations": staged_macho, "pre_install_staged_mach_o_observations_sha256": h(), "staged_mach_o_observations": staged_macho, "staged_mach_o_observations_sha256": h(), "sha256": h()},
            "r4_protected_execution_manifest.schema.json": {"kind": "R4_PROTECTED_EXECUTION_MANIFEST_V2", "namespace": ns, "authority_id": aid, "git": {"head": "a" * 40, "tree": "b" * 40, "required_blobs": []}, "entries": checkout_entries, "runtime_manifest_sha256": h(), "input_bundle_manifest_sha256": h(), "materialization_policy_sha256": h(), "sha256": h()},
            "r4_task14_privileged_materialization_review_package_v2.schema.json": {"kind": "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2", "namespace": ns, "authority_id": aid, "reviewed_head": "a" * 40, "reviewed_tree": "b" * 40, "required_blob_set_sha256": h(), "exact_suite_log_sha256": h(), "input_bundle_manifest_sha256": h(), "lock_sha256": h(), "python_archive_sha256": h(), "wheel_set_sha256": h(), "materialization_policy_sha256": h(), "provisioner_swift_source_sha256": h(), "provisioner_binary_sha256": h("3"), "provisioner_designated_requirement_sha256": h("4"), "helper_hashes": helpers, "helper_team_id": "3BMF4LM6TM", "helper_signing_identity_sha1": "4C6503D0A78A9F51C86CC7C9683115E74A4902A7", "task14_approval_verification_public_key_sha256": key_a, "sha256": h()},
            "r4_task14_privileged_materialization_observation_authorization_v2.schema.json": {"kind": "R4_TASK14_PRIVILEGED_MATERIALIZATION_OBSERVATION_AUTHORIZATION_V2", "namespace": ns, "authority_id": aid, "review_package_sha256": h(), "input_bundle_manifest_sha256": h(), "materialization_policy_sha256": h(), "provisioner_binary_sha256": h("3"), "provisioner_designated_requirement_sha256": h("4"), "task14_approval_verification_public_key_sha256": key_a, "sha256": h()},
            "r4_task14_protected_execution_approval.schema.json": {"kind": "R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2", "namespace": ns, "authority_id": aid, "reviewed_head": "a" * 40, "reviewed_tree": "b" * 40, "required_blob_set_sha256": h(), "exact_suite_log_sha256": h(), "review_package_sha256": h(), "observation_authorization_sha256": h(), "task14_approval_verification_public_key_sha256": key_a, "input_bundle_manifest_sha256": h(), "lock_sha256": h(), "python_archive_sha256": h(), "wheel_set_sha256": h(), "materialization_policy_sha256": h(), "sandbox_launcher_path": "/usr/bin/sandbox-exec", "sandbox_launcher_descriptor_identity_sha256": h("9"), "sandbox_launcher_sha256": h("a"), "sandbox_launcher_codesign_identity_sha256": h("6"), "sandbox_profile_template_sha256": h("d"), "child_command_template_sha256": h("e"), "base_interpreter_path": "runtime/closure/python311/bin/python3.11", "base_interpreter_sha256": h("b"), "base_interpreter_mach_o_sha256": h("c"), "base_interpreter_codesign_identity_sha256": h("8"), "provisioner_swift_source_sha256": h(), "provisioner_binary_sha256": h("3"), "provisioner_designated_requirement_sha256": h("4"), "helper_hashes": helpers, "helper_team_id": "3BMF4LM6TM", "helper_signing_identity_sha1": "4C6503D0A78A9F51C86CC7C9683115E74A4902A7", "sha256": h()},
            "r4_protected_execution_receipt.schema.json": {"kind": "R4_PROTECTED_EXECUTION_RECEIPT_V2", "namespace": ns, "authority_id": aid, "state": "SEALED", "key_generation": 1, "manifest_sha256": h(), "runtime_manifest_sha256": h(), "task14_approval_sha256": h(), "input_bundle_manifest_sha256": h(), "lock_sha256": h(), "materialization_policy_sha256": h(), "wheel_set_sha256": h(), "runtime_output_closure_sha256": h(), "dependency_closure_sha256": h(), "pre_install_staged_mach_o_observations_sha256": h(), "staged_mach_o_observations_sha256": h(), "admission_record_sha256": h(), "git_head": "a" * 40, "git_tree": "b" * 40, "required_blob_set_sha256": h(), "provisioner_binary_sha256": h("3"), "helper_codesign_requirement_sha256": h("4"), "helper_team_id": "3BMF4LM6TM", "helper_signing_identity_sha1": "4C6503D0A78A9F51C86CC7C9683115E74A4902A7", "public_key_sha256": h(), "published_closure_sha256": h(), "sandbox_launcher_path": "/usr/bin/sandbox-exec", "sandbox_launcher_descriptor_identity_sha256": h("9"), "sandbox_launcher_sha256": h("a"), "sandbox_launcher_codesign_identity_sha256": h("6"), "sandbox_profile_template_sha256": h("d"), "sandbox_profile_sha256": h("e"), "child_command_template_sha256": h("f"), "installer_command_sha256": h("0"), "import_command_sha256": h("1"), "interpreter_pre_install_sha256": h("b"), "interpreter_pre_install_mach_o_sha256": h("c"), "interpreter_pre_install_codesign_identity_sha256": h("8"), "interpreter_post_install_sha256": h("b"), "interpreter_post_install_mach_o_sha256": h("c"), "interpreter_post_install_codesign_identity_sha256": h("8"), "environment_sha256": h("2"), "sha256": h()},
            "r4_protected_execution_keyring.schema.json": {"kind":"R4_PROTECTED_EXECUTION_KEYRING_V1","key_id":h("b"),"generation":1,"public_key_sha256":h("b"),"previous_record_sha256":None,"issued_at":"bootstrap-generation-1","detached_signature_sha256":h(),"sha256":h()},
        }

    def test_policy_requires_locator_and_journal_schema_versions(self) -> None:
        policy = self._fixtures()["r4_privileged_materialization_policy_v2.schema.json"]
        policy["schema_versions"] = policy["schema_versions"][:3] + [
            "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2",
            "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2",
        ] + policy["schema_versions"][5:]
        validator = Draft202012Validator(
            json.loads(
                (ROOT / "schemas/r4_privileged_materialization_policy_v2.schema.json").read_text()
            )
        )
        self.assertFalse(list(validator.iter_errors(policy)))

    def test_protected_evidence_schemas_are_closed_and_reject_top_level_mutations(self):
        fixtures = self._fixtures()
        for name, fixture in fixtures.items():
            schema = json.loads((ROOT / "schemas" / name).read_text())
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            validator = Draft202012Validator(schema)
            self.assertFalse(list(validator.iter_errors(fixture)), name)
            for key in fixture:
                candidate = copy.deepcopy(fixture)
                candidate.pop(key)
                self.assertTrue(list(validator.iter_errors(candidate)), f"{name}:{key}")
            extra = copy.deepcopy(fixture)
            extra["extra"] = True
            self.assertTrue(list(validator.iter_errors(extra)), name)

        nested = {
            "r4_runtime_input_bundle_v2.schema.json": ("git",),
            "r4_privileged_materialization_policy_v2.schema.json": ("child_execution",),
            "r4_protected_execution_manifest.schema.json": ("git",),
            "r4_hermetic_python311_runtime.schema.json": ("entries", 0),
            "r4_task14_privileged_materialization_review_package_v2.schema.json": ("helper_hashes", 0),
            "r4_task14_protected_execution_approval.schema.json": ("helper_hashes", 0),
            "r4_protected_execution_receipt.schema.json": None,
            "r4_protected_execution_keyring.schema.json": None,
        }
        for name, path in nested.items():
            if path is None:
                continue
            candidate = copy.deepcopy(fixtures[name])
            target = candidate
            for key in path:
                target = target[key]
            target["extra"] = True
            self.assertFalse(Draft202012Validator(json.loads((ROOT / "schemas" / name).read_text())).is_valid(candidate), name)

    def test_v2_evidence_schemas_reject_v1_kinds_and_mixed_documents(self):
        for name, fixture in self._fixtures().items():
            if not fixture["kind"].endswith("_V2"):
                continue
            validator = Draft202012Validator(json.loads((ROOT / "schemas" / name).read_text()))
            v1 = copy.deepcopy(fixture)
            v1["kind"] = v1["kind"].replace("_V2", "_V1")
            self.assertFalse(validator.is_valid(v1), name)
            mixed = copy.deepcopy(fixture)
            mixed["legacy_v1_field"] = self._hash()
            self.assertFalse(validator.is_valid(mixed), name)

    def test_policy_codesign_identity_constraints_match_swift(self):
        fixture = self._fixtures()["r4_privileged_materialization_policy_v2.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_privileged_materialization_policy_v2.schema.json").read_text()))
        self.assertTrue(validator.is_valid(fixture))
        committed = json.loads((ROOT / "config/r4_privileged_materialization_policy.json").read_text())
        self.assertTrue(validator.is_valid(committed))

        for component in ("sandbox_launcher", "base_interpreter"):
            missing_component_field = copy.deepcopy(fixture)
            missing_component_field[component].pop("path")
            self.assertFalse(validator.is_valid(missing_component_field), f"{component}:missing path")
            extra_component_field = copy.deepcopy(fixture)
            extra_component_field[component]["extra"] = None
            self.assertFalse(validator.is_valid(extra_component_field), f"{component}:extra field")
            missing = copy.deepcopy(fixture)
            missing[component].pop("codesign_identity")
            self.assertFalse(validator.is_valid(missing), f"{component}:missing identity")
            extra = copy.deepcopy(fixture)
            extra[component]["codesign_identity"]["extra"] = None
            self.assertFalse(validator.is_valid(extra), f"{component}:extra identity field")

        for field, value in (
            ("identifier", "sandbox-exec"),
            ("designated_requirement", "identifier sandbox-exec"),
            ("team_identifier", "TEAMID"),
        ):
            candidate = copy.deepcopy(fixture)
            candidate["sandbox_launcher"]["codesign_identity"][field] = value
            self.assertFalse(validator.is_valid(candidate), f"launcher:{field}")

        for team in ("", "é"):
            candidate = copy.deepcopy(fixture)
            candidate["base_interpreter"]["codesign_identity"]["team_identifier"] = team
            self.assertFalse(validator.is_valid(candidate), f"interpreter team {team!r}")

        nonempty_ascii_team = copy.deepcopy(fixture)
        nonempty_ascii_team["base_interpreter"]["codesign_identity"]["team_identifier"] = "TEAMID"
        self.assertTrue(validator.is_valid(nonempty_ascii_team))

    def test_v2_entry_schemas_reject_path_aliases_root_and_type_mode_confusion(self):
        fixture = self._fixtures()["r4_protected_execution_manifest.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_protected_execution_manifest.schema.json").read_text()))
        for path in ("/x", "./x", "a//b", "a/../b", "a\\b", "é"):
            candidate = copy.deepcopy(fixture)
            candidate["entries"][1]["path"] = path
            self.assertTrue(list(validator.iter_errors(candidate)), path)
        candidate = copy.deepcopy(fixture)
        candidate["entries"][1].update(type="directory", mode="0444", size=1)
        self.assertTrue(list(validator.iter_errors(candidate)))
        for path, type_, mode, size in (("checkout", "regular", "0444", 0), ("checkout", "directory", "0444", 0), ("checkout", "directory", "0555", 1), ("outside", "directory", "0555", 0)):
            candidate = copy.deepcopy(fixture)
            candidate["entries"][0].update(path=path, type=type_, mode=mode, size=size)
            self.assertFalse(validator.is_valid(candidate), (path, type_, mode, size))
        for array_path in (("entries",),):
            candidate = copy.deepcopy(fixture)
            target = candidate
            for key in array_path:
                target = target[key]
            target.append(copy.deepcopy(target[0]))
            self.assertFalse(validator.is_valid(candidate), array_path)

    def test_v2_entry_schemas_preserve_executable_regular_files_only_as_0555(self):
        """Treating an executable as data would make a valid protected entry unrepresentable."""
        fixture = self._fixtures()["r4_protected_execution_manifest.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_protected_execution_manifest.schema.json").read_text()))
        executable = copy.deepcopy(fixture)
        executable["entries"][1]["mode"] = "0555"
        self.assertTrue(validator.is_valid(executable))
        for mode in ("0400", "0644", "0755", "0000"):
            candidate = copy.deepcopy(fixture)
            candidate["entries"][1]["mode"] = mode
            self.assertFalse(validator.is_valid(candidate), mode)

    def test_v2_bundle_authority_and_runtime_require_typed_root_entries(self):
        cases = (
            ("r4_runtime_input_bundle_v2.schema.json", "checkout_entries", "checkout"),
            ("r4_protected_execution_manifest.schema.json", "entries", "checkout"),
            ("r4_hermetic_python311_runtime.schema.json", "entries", "runtime"),
        )
        fixtures = self._fixtures()
        for name, field, root in cases:
            with self.subTest(name=name):
                validator = Draft202012Validator(json.loads((ROOT / "schemas" / name).read_text()))
                self.assertTrue(validator.is_valid(fixtures[name]))
                for mutation in (
                    {"path": root, "type": "regular", "mode": "0444", "size": 0},
                    {"path": root, "type": "directory", "mode": "0444", "size": 0},
                    {"path": root, "type": "directory", "mode": "0555", "size": 1},
                    {"path": "outside", "type": "directory", "mode": "0555", "size": 0},
                ):
                    candidate = copy.deepcopy(fixtures[name])
                    candidate[field][0].update(mutation)
                    self.assertFalse(validator.is_valid(candidate), mutation)

    def test_bundle_artifact_components_reject_aliases_and_separators(self):
        fixture = self._fixtures()["r4_runtime_input_bundle_v2.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_runtime_input_bundle_v2.schema.json").read_text()))
        for filename in (".", "..", "", "wheel/name", r"wheel\name"):
            candidate = copy.deepcopy(fixture)
            candidate["artifacts"][1]["filename"] = filename
            self.assertFalse(validator.is_valid(candidate), filename)
        for path in ("artifacts/.", "artifacts/..", "artifacts/", "artifacts/wheel/name", r"artifacts/wheel\name"):
            candidate = copy.deepcopy(fixture)
            candidate["artifacts"][1]["path"] = path
            self.assertFalse(validator.is_valid(candidate), path)

    def test_bundle_schema_allows_only_v1_derived_empty_wheel_versions(self):
        fixture = self._fixtures()["r4_runtime_input_bundle_v2.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_runtime_input_bundle_v2.schema.json").read_text()))
        wheel = copy.deepcopy(fixture)
        wheel["artifacts"][1]["version"] = ""
        self.assertTrue(validator.is_valid(wheel))
        archive = copy.deepcopy(fixture)
        archive["artifacts"][0]["version"] = ""
        self.assertFalse(validator.is_valid(archive))
        non_ascii = copy.deepcopy(fixture)
        non_ascii["artifacts"][1]["version"] = "é"
        self.assertFalse(validator.is_valid(non_ascii))

    def test_runtime_and_approval_reject_path_aliases_and_untrusted_macho_locations(self):
        fixtures = self._fixtures()
        runtime_schema = Draft202012Validator(json.loads((ROOT / "schemas/r4_hermetic_python311_runtime.schema.json").read_text()))
        approval_schema = Draft202012Validator(json.loads((ROOT / "schemas/r4_task14_protected_execution_approval.schema.json").read_text()))
        runtime = fixtures["r4_hermetic_python311_runtime.schema.json"]
        runtime["mach_o_dependencies"] = [{"loader":"runtime/closure/python311/bin/python3.11", "dependency":"/usr/lib/libSystem.B.dylib", "resolved":"/usr/lib/libSystem.B.dylib"}]
        self.assertTrue(runtime_schema.is_valid(runtime))
        approval = fixtures["r4_task14_protected_execution_approval.schema.json"]
        self.assertTrue(approval_schema.is_valid(approval))
        aliases = ("/x", "./x", "a//b", "a/../b", "a/", r"a\\b", "é")
        for alias in aliases:
            with self.subTest(alias=alias):
                candidate = copy.deepcopy(runtime)
                candidate["mach_o_dependencies"][0]["dependency"] = alias
                self.assertFalse(runtime_schema.is_valid(candidate))
                candidate = copy.deepcopy(runtime)
                candidate["mach_o_dependencies"][0]["resolved"] = alias
                self.assertFalse(runtime_schema.is_valid(candidate))
                candidate = copy.deepcopy(approval)
                candidate["helper_hashes"][0]["path"] = alias
                self.assertFalse(approval_schema.is_valid(candidate))
        for loader in ("../x", "runtime/../x", "runtime/./x", "runtime//x"):
            candidate = copy.deepcopy(runtime)
            candidate["mach_o_dependencies"][0]["loader"] = loader
            self.assertFalse(runtime_schema.is_valid(candidate), loader)
        for dependency in ("@loader_path", "@loader_path/lib/libx.dylib", "@executable_path/lib/libx.dylib", "@rpath/lib/libx.dylib", "/System/Library/Frameworks/X.framework/X"):
            with self.subTest(dependency=dependency):
                candidate = copy.deepcopy(runtime)
                candidate["mach_o_dependencies"][0]["dependency"] = dependency
                self.assertTrue(runtime_schema.is_valid(candidate))
        for resolved in ("/usr/lib/libSystem.B.dylib", "/System/Library/Frameworks/X.framework/X"):
            candidate = copy.deepcopy(runtime)
            candidate["mach_o_dependencies"][0]["resolved"] = resolved
            self.assertTrue(runtime_schema.is_valid(candidate))
        for invalid in ("@loader_path/../evil", "@loader_path/", "/usr/lib/.", "/usr/lib/..", "/usr/lib/../evil", "/usr/lib//x", "/usr/lib/", "/System/.", "/System/..", "/System//x", "/System/", "//usr/lib/x", "/System/é", "/Library/Application Support/mlb-walkforward-lab/protected-execution-authority/lib/x"):
            candidate = copy.deepcopy(runtime)
            candidate["mach_o_dependencies"][0]["dependency"] = invalid
            self.assertFalse(runtime_schema.is_valid(candidate), invalid)
            candidate = copy.deepcopy(runtime)
            candidate["mach_o_dependencies"][0]["resolved"] = invalid
            self.assertFalse(runtime_schema.is_valid(candidate), invalid)
        for fixture, field in ((runtime, "mach_o_dependencies"), (approval, "helper_hashes")):
            candidate = copy.deepcopy(fixture)
            candidate[field].append(copy.deepcopy(candidate[field][0]))
            validator = runtime_schema if field == "mach_o_dependencies" else approval_schema
            self.assertFalse(validator.is_valid(candidate))

    def test_runtime_staged_macho_schema_is_closed_and_typed(self):
        """Catches a schema that accepts a placeholder `{}` rather than Swift's full observation."""
        runtime = self._fixtures()["r4_hermetic_python311_runtime.schema.json"]
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_hermetic_python311_runtime.schema.json").read_text()))
        self.assertTrue(validator.is_valid(runtime))
        for mutation in (
            lambda row: row.pop("codesign_identity_sha256"),
            lambda row: row.__setitem__("extra", True),
            lambda row: row.__setitem__("sha256", 1),
            lambda row: row["mach_o_observation"].__setitem__("extra", True),
            lambda row: row["mach_o_observation"]["slices"][0].__setitem__("uuid", "not-a-uuid"),
            lambda row: row["mach_o_observation"]["dependencies"].append({"loader": "runtime/closure/python311/bin/python3.11", "dependency": "/tmp/evil.dylib", "resolved": "/tmp/evil.dylib"}),
        ):
            candidate = copy.deepcopy(runtime)
            mutation(candidate["staged_mach_o_observations"][0])
            self.assertFalse(validator.is_valid(candidate))

    def test_keyring_is_closed_to_key_identified_genesis_rotation_and_revocation(self):
        validator = Draft202012Validator(json.loads((ROOT / "schemas/r4_protected_execution_keyring.schema.json").read_text()))
        h = self._hash
        genesis = self._fixtures()["r4_protected_execution_keyring.schema.json"]
        rotation_v1 = {"kind":"R4_PROTECTED_EXECUTION_ROTATION_V1","key_id":h("c"),"generation":2,"previous_record_sha256":h("d"),"previous_public_key_sha256":h("e"),"new_public_key_sha256":h("f"),"issued_at":"2026-08-08T00:00:00Z","detached_signature_sha256":h(),"sha256":h()}
        revocation_v1 = {"kind":"R4_PROTECTED_EXECUTION_REVOCATION_V1","key_id":h("c"),"namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","authority_id":"r4-disjoint-custody-v2-execution-v1","state":"REVOKED","sequence":1,"key_generation":1,"public_key_sha256":h(),"previous_record_sha256":h("d"),"issued_at":"2026-08-08T00:00:00Z","revoked_at":"2026-08-08T00:00:01Z","revocation_reason":"KEY_COMPROMISE","detached_signature_sha256":h(),"sha256":h()}
        rotation_v2 = {"kind":"R4_PROTECTED_EXECUTION_KEY_ROTATION_V2","authority_id":"r4-disjoint-custody-v2-execution-v1","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","generation":2,"previous_generation":1,"previous_record_sha256":h("d"),"previous_public_key_sha256":h("e"),"new_public_key_sha256":h("f"),"issued_at":"2026-08-08T00:00:00Z","detached_signature_sha256":h(),"sha256":h()}
        revocation_v2 = {"kind":"R4_PROTECTED_EXECUTION_REVOCATION_V2","authority_id":"r4-disjoint-custody-v2-execution-v1","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","state":"REVOKED","sequence":1,"signer_generation":1,"signer_key_record_sha256":h("d"),"signer_public_key_sha256":h("e"),"target_generation":1,"target_key_record_sha256":h("d"),"target_public_key_sha256":h("e"),"previous_revocation_sha256":None,"reason":"KEY_COMPROMISE","revoked_at":"2026-08-08T00:00:01Z","detached_signature_sha256":h(),"sha256":h()}
        for label, fixture in (("genesis", genesis), ("rotation-v2", rotation_v2), ("revocation-v2", revocation_v2)):
            with self.subTest(label=label):
                self.assertTrue(validator.is_valid(fixture))
                for required in fixture:
                    candidate = copy.deepcopy(fixture)
                    candidate.pop(required)
                    self.assertFalse(validator.is_valid(candidate), f"{label}:{required}")
        self.assertFalse(validator.is_valid(rotation_v1))
        self.assertFalse(validator.is_valid(revocation_v1))
        for fixture, mutations in (
            (rotation_v2, (("generation", True), ("kind", "R4_PROTECTED_EXECUTION_ROTATION_V1"), ("namespace", "wrong"), ("extra", None))),
            (revocation_v2, (("signer_generation", True), ("state", "ACTIVE"), ("reason", "OPEN"), ("previous_revocation_sha256", h("a")), ("extra", None))),
        ):
            for key, value in mutations:
                candidate = copy.deepcopy(fixture)
                candidate[key] = value
                self.assertFalse(validator.is_valid(candidate), f"{key}:{value}")

    def test_all_protected_schemas_have_unique_ids_and_are_recursively_closed(self):
        names = tuple(self._fixtures())
        schemas = {name: json.loads((ROOT / "schemas" / name).read_text()) for name in names}
        self.assertEqual({schema["$schema"] for schema in schemas.values()}, {"https://json-schema.org/draft/2020-12/schema"})
        self.assertEqual({schema["$id"] for schema in schemas.values()}, set(names))
        self.assertIn("path", schemas["r4_protected_execution_manifest.schema.json"]["$defs"])
        self.assertIn("path", schemas["r4_hermetic_python311_runtime.schema.json"]["$defs"])
        runtime_constraints = schemas["r4_hermetic_python311_runtime.schema.json"]["properties"]["mach_o_dependencies"]
        self.assertEqual(runtime_constraints["x-r4-semanticConstraints"], ["strict-loader-dependency-resolved-order", "R4V2_RUNTIME_DEPENDENCIES"])

        def assert_closed(node, name):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node.get("additionalProperties", True), name)
                for value in node.values():
                    assert_closed(value, name)
            elif isinstance(node, list):
                for value in node:
                    assert_closed(value, name)

        for name, schema in schemas.items():
            assert_closed(schema, name)


if __name__ == "__main__":
    unittest.main()
