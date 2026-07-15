from __future__ import annotations

import dataclasses
import contextlib
import hashlib
import inspect
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import jsonschema

from mlb_backtest import v52_evidence
from mlb_backtest.manifest import RawStore
from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    PublishedEvidence,
    V52EvidenceStore,
    canonical_query_bytes,
    logical_query_id,
)
from mlb_backtest.v52_protected_inventory import build_protected_raw_inventory


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = ROOT / "schemas/v5_2_evidence_manifest.schema.json"
SIDECAR_SCHEMA = ROOT / "schemas/v5_2_query_sidecar.schema.json"
DERIVED_SIDECAR_SCHEMA = (
    ROOT / "schemas/v5_2_derived_disposition_sidecar.schema.json"
)


def _query(**changes: object) -> LogicalQuery:
    values: dict[str, object] = {
        "source": "mlb_schedule",
        "grain": "schedule_anchor",
        "endpoint": "https://statsapi.mlb.com/api/v1/schedule",
        "parameters": {"sportId": 1, "hydrate": "venue,lineups"},
        "requested_range": {
            "inclusive_start": "2022-04-07",
            "inclusive_end": "2022-04-08",
        },
        "expected_media_type": "application/json",
        "parser_version": "schedule-v1",
        "parent_query_id": None,
        "run_spec_sha256": "a" * 64,
        "acquisition_contract_sha256": "b" * 64,
    }
    values.update(changes)
    return LogicalQuery(**values)  # type: ignore[arg-type]


def _capture(
    body: bytes | None = b'{"dates":[]}',
    *,
    status: int | None = 200,
    error: str | None = None,
) -> AttemptCapture:
    return AttemptCapture(
        started_at_utc="2026-07-14T12:00:00.000001Z",
        ended_at_utc="2026-07-14T12:00:01.000001Z",
        final_url="https://statsapi.mlb.com/api/v1/schedule?sportId=1",
        http_status=status,
        response_headers=(
            ("X-Ignored", "no"),
            ("content-type", "application/json; charset=utf-8"),
            ("CONTENT-LENGTH", str(len(body or b""))),
            ("Etag", '"fixture"'),
            ("Last-Modified", "Tue, 14 Jul 2026 12:00:00 GMT"),
            ("Retry-After", "3"),
        ),
        body=body,
        error_classification=error,
    )


def _sidecar() -> dict[str, object]:
    return {
        "coverage": {
            "inclusive_end": "2022-04-08",
            "inclusive_start": "2022-04-07",
        },
        "game_pks": [1, 2],
        "status": "READY",
    }


def _derived_query(**changes: object) -> LogicalQuery:
    values: dict[str, object] = {
        "source": "mlb_stats_pitching",
        "grain": "season_d1_league_pitching_reference",
        "endpoint": "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
        "parameters": {},
        "requested_range": {
            "as_of_date": "2024-03-19",
            "consumer_cutoff_dates": {"9001": "2024-03-19"},
            "consuming_game_pks": [9001],
            "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
            "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
            "season": 2024,
        },
        "expected_media_type": "application/json",
        "parser_version": "v5.2.1",
        "parent_query_id": None,
        "run_spec_sha256": "a" * 64,
        "acquisition_contract_sha256": "b" * 64,
    }
    values.update(changes)
    return LogicalQuery(**values)  # type: ignore[arg-type]


def _derived_semantics() -> dict[str, object]:
    return {
        "as_of_date": "2024-03-19",
        "consumer_cutoff_dates": {"9001": "2024-03-19"},
        "consuming_game_pks": [9001],
        "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
        "season": 2024,
    }


def _canonical_file(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _tmp(value: str) -> Path:
    return Path(value).resolve()


def _legacy_fixture(
    base: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, dict[str, object]]:
    parameters = {
        "sportId": 1,
        "gameType": "R",
        "startDate": "2022-04-07",
        "endDate": "2022-10-05",
        "hydrate": "probablePitcher,lineups,venue,linescore",
    }
    current_query = dataclasses.replace(
        _query(),
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        parameters=parameters,
        requested_range={
            "year": 2022,
            "start_date": "2022-04-07",
            "end_date": "2022-10-05",
        },
        parser_version="v5.2.1",
    )
    legacy_query = {
        "end": "2022-10-05",
        "gameType": "R",
        "role": "training",
        "start": "2022-04-07",
        "url": (
            "https://statsapi.mlb.com/api/v1/schedule?"
            + urlencode(tuple(parameters.items()))
        ),
    }
    return _legacy_reference_fixture(
        base,
        source="mlb_schedule",
        legacy_query=legacy_query,
        current_query=current_query,
        raw=b"legacy-one\nlegacy-two\n",
    )


def _legacy_reference_fixture(
    base: Path,
    *,
    source: str,
    legacy_query: dict[str, object],
    current_query: LogicalQuery | Callable[[str], LogicalQuery],
    raw: bytes,
) -> tuple[V52EvidenceStore, LogicalQuery, dict[str, object]]:
    repository = base / "repo"
    repository.mkdir()
    legacy_root = repository / "data/v5_1_bt/raw"
    artifact = RawStore(legacy_root).put(source, legacy_query, raw)
    inventory = build_protected_raw_inventory(repository, legacy_root)
    inventory_path = repository / "audit/v5_1_bt/protected_raw_inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_bytes(_canonical_file(inventory))
    inventory_sha256 = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    if callable(current_query):
        current_query = current_query(inventory_sha256)
    record = inventory["records"][0]
    manifest = json.loads(artifact.manifest_path.read_bytes())
    reference = {
        "canonical_query": record["canonical_query"],
        "current_query_id": logical_query_id(current_query),
        "legacy_source": source,
        "legacy_manifest_path": record["manifest"]["path"],
        "legacy_manifest_sha256": record["manifest"]["sha256"],
        "legacy_seal_path": record["seal"]["path"],
        "legacy_seal_sha256": record["seal"]["sha256"],
        "parser_version": current_query.parser_version,
        "payload_path": record["raw"]["path"],
        "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json",
        "protected_inventory_sha256": inventory_sha256,
        "raw_byte_count": record["raw"]["byte_size"],
        "raw_row_count": manifest["row_count"],
        "raw_sha256": record["raw"]["sha256"],
        "semantic_sha256": hashlib.sha256(
            json.dumps(
                _sidecar(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "source": current_query.source,
    }
    evidence_root = repository / "data/v5_2_qd/historical/raw"
    evidence_root.parent.mkdir(parents=True)
    return V52EvidenceStore(evidence_root), current_query, reference


def _statcast_reference_fixture(
    base: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, dict[str, object]]:
    first = last = "2024-04-02"
    parameters = {
        "all": "true",
        "type": "details",
        "player_type": "batter",
        "game_date_gt": first,
        "game_date_lt": last,
        "hfGT": "R|",
        "group_by": "name-year",
        "sort_col": "pitches",
        "player_event_sort": "api_p_release_speed",
        "sort_order": "desc",
        "min_pitches": "0",
    }

    def current(inventory_sha256: str) -> LogicalQuery:
        child = LogicalQuery(
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            parameters=parameters,
            requested_range={
                "year": 2024,
                "start_date": first,
                "end_date": last,
                "acquisition": "PROTECTED_LEGACY_REFERENCE",
                "protected_inventory_sha256": inventory_sha256,
            },
            expected_media_type="text/csv",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        parent = dataclasses.replace(
            child,
            parameters={
                **parameters,
                "game_date_gt": "2024-04-01",
                "game_date_lt": "2024-04-30",
            },
            requested_range={
                "year": 2024,
                "start_date": "2024-04-01",
                "end_date": "2024-04-30",
            },
        )
        return dataclasses.replace(child, parent_query_id=logical_query_id(parent))

    legacy_query = {
        "start": first,
        "end": last,
        "game_type": "R",
        "url": (
            "https://baseballsavant.mlb.com/statcast_search/csv?"
            + urlencode(tuple(parameters.items()))
        ),
    }
    return _legacy_reference_fixture(
        base,
        source="baseball_savant_statcast",
        legacy_query=legacy_query,
        current_query=current,
        raw=b"game_date,game_pk\n2024-04-02,1\n",
    )


def _d1_reference_fixture(
    base: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, dict[str, object]]:
    parameters = {
        "stats": "byDateRange",
        "group": "pitching",
        "sportIds": 1,
        "gameType": "R",
        "playerPool": "ALL",
        "limit": 5000,
        "startDate": "2024-03-20",
        "endDate": "2024-04-01",
    }
    query = LogicalQuery(
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint="https://statsapi.mlb.com/api/v1/stats",
        parameters=parameters,
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
    return _legacy_reference_fixture(
        base,
        source=query.source,
        legacy_query={
            **parameters,
            "url": query.endpoint + "?" + urlencode(tuple(parameters.items())),
        },
        current_query=query,
        raw=b'{"stats":[]}\n',
    )


def _career_reference_fixture(
    base: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, dict[str, object]]:
    parameters = {
        "stats": "byDateRange",
        "group": "pitching",
        "gameType": "R",
        "sportIds": 1,
        "startDate": "1876-01-01",
        "endDate": "2017-12-31",
    }
    query = LogicalQuery(
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
        parameters=parameters,
        requested_range={
            "pitcher_id": 101,
            "inclusive_end": "2017-12-31",
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": "2021-12-31"},
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    return _legacy_reference_fixture(
        base,
        source=query.source,
        legacy_query={
            **parameters,
            "pitcher_id": 101,
            "url": query.endpoint + "?" + urlencode(tuple(parameters.items())),
        },
        current_query=query,
        raw=b'{"stats":[]}\n',
    )


class V52EvidenceTest(unittest.TestCase):
    def _read_progress(
        self, store: V52EvidenceStore, query: LogicalQuery
    ) -> object:
        self.assertTrue(
            hasattr(store, "read_verified_query_progress"),
            "verified query progress API must exist",
        )
        return store.read_verified_query_progress(query)  # type: ignore[attr-defined]

    def test_task6_publisher_requires_exact_source_specific_legacy_relation(
        self,
    ) -> None:
        fixtures = (
            ("schedule", _legacy_fixture),
            ("statcast", _statcast_reference_fixture),
            ("d1", _d1_reference_fixture),
            ("career", _career_reference_fixture),
        )
        for label, fixture in fixtures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                store, query, reference = fixture(_tmp(td))
                with store.acquire_query(query):
                    published = store.publish_legacy_reference(
                        query, reference, _sidecar()
                    )
                self.assertEqual(published.query_id, logical_query_id(query))

                if label == "schedule":
                    mutations = (
                        dataclasses.replace(
                            query,
                            parameters={**query.parameters, "sportId": 2},
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "end_date": "2022-10-04",
                            },
                        ),
                    )
                elif label == "statcast":
                    mutations = (
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "start_date": "2024-04-03",
                            },
                        ),
                        dataclasses.replace(query, parent_query_id="c" * 64),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "protected_inventory_sha256": "d" * 64,
                            },
                        ),
                    )
                elif label == "d1":
                    mutations = (
                        dataclasses.replace(
                            query,
                            parameters={**query.parameters, "limit": 4999},
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "as_of_date": "2024-04-02",
                            },
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "consumer_cutoff_dates": {"9001": "2024-03-31"},
                            },
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "consuming_game_pks": [9002],
                                "consumer_cutoff_dates": {"9002": "2024-04-01"},
                            },
                        ),
                    )
                else:
                    mutations = (
                        dataclasses.replace(
                            query,
                            endpoint=(
                                "https://statsapi.mlb.com/api/v1/people/102/stats"
                            ),
                            requested_range={
                                **query.requested_range,
                                "pitcher_id": 102,
                            },
                        ),
                        dataclasses.replace(
                            query,
                            parameters={
                                **query.parameters,
                                "endDate": "2021-12-31",
                            },
                            requested_range={
                                **query.requested_range,
                                "inclusive_end": "2021-12-31",
                            },
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "consumer_cutoff_dates": {"9001": "2022-01-01"},
                            },
                        ),
                        dataclasses.replace(
                            query,
                            requested_range={
                                **query.requested_range,
                                "consuming_game_pks": [9002],
                                "consumer_cutoff_dates": {"9002": "2021-12-31"},
                            },
                        ),
                    )
                for changed in mutations:
                    with self.subTest(label=label, changed=changed):
                        success = (
                            store.root
                            / "queries"
                            / changed.source
                            / logical_query_id(changed)
                            / "success.json"
                        )
                        with store.acquire_query(changed), self.assertRaises(ValueError):
                            store.publish_legacy_reference(
                                changed, reference, _sidecar()
                            )
                        self.assertFalse(success.exists())

        with tempfile.TemporaryDirectory() as td:
            parameters = {
                "sportId": 1,
                "gameType": "R",
                "startDate": "2022-04-07",
                "endDate": "2022-10-05",
                "hydrate": "probablePitcher,lineups,venue,linescore",
            }
            query = dataclasses.replace(
                _query(),
                source="mlb_schedule_game_feed",
                grain="season_schedule_and_game",
                parameters=parameters,
                requested_range={
                    "year": 2022,
                    "start_date": "2022-04-07",
                    "end_date": "2022-10-05",
                },
                parser_version="v5.2.1",
            )
            store, query, reference = _legacy_reference_fixture(
                _tmp(td),
                source="mlb_schedule",
                legacy_query={
                    "end": "2022-10-05",
                    "gameType": "R",
                    "role": "training",
                    "start": "2022-04-07",
                    "url": query.endpoint + "?sportId=2",
                },
                current_query=query,
                raw=b"legacy\n",
            )
            with store.acquire_query(query), self.assertRaises(ValueError):
                store.publish_legacy_reference(query, reference, _sidecar())

    def test_quality_i5_schedule_alias_requires_exact_current_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            forbidden = dataclasses.replace(
                query,
                source="mlb_schedule_game_feed",
                grain="game_boxscore",
                endpoint="https://statsapi.mlb.com/api/v1/game/1/boxscore",
            )
            reference["source"] = forbidden.source
            reference["current_query_id"] = logical_query_id(forbidden)
            with store.acquire_query(forbidden), self.assertRaisesRegex(
                ValueError, "source|alias|binding|contract"
            ):
                store.publish_legacy_reference(forbidden, reference, _sidecar())

    def test_legacy_reference_allows_only_closed_schedule_source_alias(self) -> None:
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            current = replace(query, source="mlb_schedule_game_feed")
            reference["source"] = current.source
            with store.acquire_query(current):
                published = store.publish_legacy_reference(
                    current, reference, _sidecar()
                )
            self.assertEqual(published.query_id, logical_query_id(current))
            self.assertEqual(store.read_verified_success(current).registered_query, current)

            forbidden = replace(query, source="mlb_stats_pitching")
            reference["source"] = forbidden.source
            reference["current_query_id"] = logical_query_id(forbidden)
            with store.acquire_query(forbidden), self.assertRaisesRegex(
                ValueError, "source|alias|binding"
            ):
                store.publish_legacy_reference(forbidden, reference, _sidecar())

    def test_acquire_query_has_exact_context_manager_return_annotation(self) -> None:
        annotation = inspect.signature(V52EvidenceStore.acquire_query).return_annotation
        self.assertEqual(annotation, "ContextManager[QueryLease]")

    def test_public_records_are_frozen_with_exact_fields(self) -> None:
        from mlb_backtest.v52_evidence import VerifiedEvidenceRead

        expected = {
            LogicalQuery: (
                "source",
                "grain",
                "endpoint",
                "parameters",
                "requested_range",
                "expected_media_type",
                "parser_version",
                "parent_query_id",
                "run_spec_sha256",
                "acquisition_contract_sha256",
            ),
            AttemptCapture: (
                "started_at_utc",
                "ended_at_utc",
                "final_url",
                "http_status",
                "response_headers",
                "body",
                "error_classification",
                "timed_out",
            ),
            PublishedEvidence: (
                "query_id",
                "success_receipt_path",
                "raw_sha256",
                "query_sidecar_sha256",
                "storage_mode",
            ),
            VerifiedEvidenceRead: (
                "registered_query",
                "published_evidence",
                "raw_bytes",
                "query_sidecar",
                "success_receipt",
                "attempt_receipts",
            ),
        }
        for record, fields in expected.items():
            with self.subTest(record=record.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(record)), fields
                )
                self.assertTrue(record.__dataclass_params__.frozen)

    def test_timed_out_capture_invariant_is_closed(self) -> None:
        capture = dataclasses.replace(
            _capture(None, status=None, error="TRANSIENT_NETWORK"),
            final_url=None,
            response_headers=(),
            timed_out=True,
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    outcome="TRANSIENT_NETWORK",
                )
                partial = dataclasses.replace(
                    _capture(b"partial", error="TRANSIENT_NETWORK"),
                    timed_out=True,
                )
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    partial,
                    outcome="TRANSIENT_NETWORK",
                )
                for invalid in (
                    dataclasses.replace(capture, body=b"partial"),
                    dataclasses.replace(capture, http_status=503),
                    dataclasses.replace(capture, final_url=query.endpoint),
                    dataclasses.replace(capture, response_headers=(("ETag", "x"),)),
                    dataclasses.replace(capture, error_classification="TIMEOUT"),
                    dataclasses.replace(partial, final_url=None),
                    dataclasses.replace(partial, http_status=None),
                ):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(
                        ValueError, "timed_out|bodyless transient"
                    ):
                        store.record_attempt(
                            query,
                            store.next_attempt_number(lease),
                            invalid,
                            outcome="TRANSIENT_NETWORK",
                        )

    def test_verified_query_progress_records_are_frozen_exported_and_exact(self) -> None:
        self.assertTrue(hasattr(v52_evidence, "VerifiedQuarantineRead"))
        self.assertTrue(hasattr(v52_evidence, "VerifiedQueryProgress"))
        self.assertTrue(hasattr(v52_evidence, "PersistedAttemptEvidence"))
        quarantine_record = v52_evidence.VerifiedQuarantineRead
        progress_record = v52_evidence.VerifiedQueryProgress
        expected = {
            v52_evidence.PersistedAttemptEvidence: (
                "query_id",
                "attempt_number",
                "outcome",
                "attempt_receipt_path",
                "attempt_receipt_sha256",
                "evidence_receipt_path",
                "evidence_receipt_sha256",
                "body_path",
                "body_sha256",
                "byte_count",
                "error_classification",
            ),
            quarantine_record: (
                "attempt_number",
                "receipt_path",
                "receipt_sha256",
                "raw_path",
                "raw_sha256",
                "raw_byte_count",
                "error_classification",
            ),
            progress_record: (
                "query_id",
                "status",
                "registered_query",
                "published_evidence",
                "attempt_receipts",
                "pending_attempt_numbers",
                "quarantines",
            ),
        }
        for record, fields in expected.items():
            with self.subTest(record=record.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(record)), fields
                )
                self.assertTrue(record.__dataclass_params__.frozen)
                self.assertIn(record.__name__, v52_evidence.__all__)

    def test_verified_query_progress_absent_query_is_read_only_pending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            progress = self._read_progress(store, query)
            after = sorted(path.relative_to(root) for path in root.rglob("*"))

            self.assertEqual(progress.query_id, logical_query_id(query))
            self.assertEqual(progress.status, "PENDING")
            self.assertIsNone(progress.registered_query)
            self.assertIsNone(progress.published_evidence)
            self.assertEqual(progress.attempt_receipts, ())
            self.assertEqual(progress.pending_attempt_numbers, ())
            self.assertEqual(progress.quarantines, ())
            self.assertEqual(after, before)

    def test_verified_query_progress_restarts_when_query_registers_after_absence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            registering_store = V52EvidenceStore(root)
            query = _query()
            original = store._query_fds
            calls = 0

            def register_after_first_miss(*args: object, **kwargs: object):
                nonlocal calls
                calls += 1
                if calls == 1:
                    registering_store.register_query(query)
                    raise FileNotFoundError("injected initial absence")
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(
                store, "_query_fds", side_effect=register_after_first_miss
            ):
                progress = self._read_progress(store, query)
            self.assertGreaterEqual(calls, 2)
            self.assertEqual(progress.status, "PENDING")
            self.assertEqual(progress.registered_query, query)

    def test_verified_query_progress_rechecks_orphan_after_absent_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            query_id = logical_query_id(query)
            original = store._quarantine_fds
            calls = 0

            def hide_first_orphan(*args: object, **kwargs: object):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (root / "quarantine" / query.source / query_id).mkdir(parents=True)
                    raise FileNotFoundError("injected quarantine absence")
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(
                    store, "_quarantine_fds", side_effect=hide_first_orphan
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "quarantine|orphan|registered"
                ),
            ):
                self._read_progress(store, query)
            self.assertGreaterEqual(calls, 2)

    def test_absent_probe_uses_one_root_generation_before_registered_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = _tmp(td)
            first_root = base / "generation-a"
            second_root = base / "generation-b"
            store = V52EvidenceStore(first_root)
            second_store = V52EvidenceStore(second_root)
            query = _query()
            second_store.register_query(query)
            root_sequence = [first_root, second_root, second_root, second_root]
            root_calls = 0
            original_quarantine = store._quarantine_fds
            observed_root_descriptors: list[int | None] = []

            def alternating_root() -> int:
                nonlocal root_calls
                selected = (
                    root_sequence[root_calls]
                    if root_calls < len(root_sequence)
                    else second_root
                )
                root_calls += 1
                return v52_evidence._open_root_path(selected, create=False)

            def observe_quarantine_root(*args: object, **kwargs: object):
                observed_root_descriptors.append(kwargs.get("root_descriptor"))
                return original_quarantine(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(store, "_root_fd", side_effect=alternating_root),
                patch.object(
                    store,
                    "_quarantine_fds",
                    side_effect=observe_quarantine_root,
                ),
            ):
                progress = self._read_progress(store, query)
            self.assertEqual(progress.registered_query, query)
            self.assertTrue(observed_root_descriptors)
            self.assertTrue(all(fd is not None for fd in observed_root_descriptors))

    def test_absent_probe_uses_one_root_generation_before_orphan_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = _tmp(td)
            first_root = base / "generation-a"
            second_root = base / "generation-b"
            store = V52EvidenceStore(first_root)
            query = _query()
            query_id = logical_query_id(query)
            second_root.mkdir()
            (second_root / "quarantine" / query.source / query_id).mkdir(parents=True)
            root_sequence = [first_root, second_root, second_root, second_root]
            root_calls = 0
            original_quarantine = store._quarantine_fds
            observed_root_descriptors: list[int | None] = []

            def alternating_root() -> int:
                nonlocal root_calls
                selected = (
                    root_sequence[root_calls]
                    if root_calls < len(root_sequence)
                    else second_root
                )
                root_calls += 1
                return v52_evidence._open_root_path(selected, create=False)

            def observe_quarantine_root(*args: object, **kwargs: object):
                observed_root_descriptors.append(kwargs.get("root_descriptor"))
                return original_quarantine(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(store, "_root_fd", side_effect=alternating_root),
                patch.object(
                    store,
                    "_quarantine_fds",
                    side_effect=observe_quarantine_root,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "quarantine|orphan|root|stable"
                ),
            ):
                self._read_progress(store, query)
            self.assertTrue(observed_root_descriptors)
            self.assertTrue(all(fd is not None for fd in observed_root_descriptors))

    def test_verified_query_progress_pending_is_reservations_minus_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                self.assertEqual(store.next_attempt_number(lease), 1)
                transient = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    transient,
                    dataclasses.replace(
                        _capture(None, status=None, error="TRANSIENT_NETWORK"),
                        final_url=None,
                        response_headers=(),
                        timed_out=True,
                    ),
                    outcome="TRANSIENT_NETWORK",
                )
                split = store.next_attempt_number(lease)
                store.persist_split_attempt(
                    query,
                    split,
                    dataclasses.replace(
                        _capture(None, status=None, error="TRANSIENT_NETWORK"),
                        final_url=None,
                        response_headers=(),
                        timed_out=True,
                    ),
                    reason="TIMEOUT",
                )
                permanent = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    permanent,
                    _capture(b"not found", status=404, error="NOT_FOUND"),
                    outcome="PERMANENT_HTTP",
                )

            progress = self._read_progress(store, query)
            self.assertEqual(progress.status, "PENDING")
            self.assertEqual(progress.registered_query, query)
            self.assertEqual(progress.pending_attempt_numbers, (1,))
            self.assertEqual(
                tuple(record["attempt_number"] for record in progress.attempt_receipts),
                (2, 3, 4),
            )
            self.assertEqual(progress.quarantines, ())

    def test_verified_query_progress_quarantine_then_success_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            quarantined_body = b"invalid-but-owned"
            with store.acquire_query(query) as lease:
                quarantined = store.next_attempt_number(lease)
                quarantine_path = store.quarantine_attempt(
                    query,
                    quarantined,
                    _capture(quarantined_body),
                    error_classification="SEMANTIC_INVALID",
                )

            progress = self._read_progress(store, query)
            self.assertEqual(progress.status, "QUARANTINED")
            self.assertIsNone(progress.published_evidence)
            self.assertEqual(len(progress.quarantines), 1)
            quarantine = progress.quarantines[0]
            self.assertEqual(quarantine.attempt_number, 1)
            self.assertEqual(quarantine.receipt_path, quarantine_path)
            self.assertEqual(
                quarantine.receipt_sha256,
                hashlib.sha256(quarantine_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(quarantine.raw_path.read_bytes(), quarantined_body)
            self.assertEqual(quarantine.raw_byte_count, len(quarantined_body))
            self.assertEqual(quarantine.error_classification, "SEMANTIC_INVALID")

            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b'{"dates":[]}'),
                    _sidecar(),
                )
            progress = self._read_progress(store, query)
            self.assertEqual(progress.status, "SUCCESS")
            self.assertEqual(progress.published_evidence, published)
            self.assertEqual(tuple(q.attempt_number for q in progress.quarantines), (1,))

    def test_verified_query_progress_streams_shared_quarantine_body_once_per_pass(
        self,
    ) -> None:
        self.assertTrue(hasattr(v52_evidence, "_read_streamed_regular_snapshot_at"))
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            body = b"x" * (2 * 1024 * 1024 + 137)
            with store.acquire_query(query) as lease:
                for _ in range(2):
                    store.quarantine_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(body),
                        error_classification="SEMANTIC_INVALID",
                    )

            original_stream = v52_evidence._read_streamed_regular_snapshot_at
            original_read = v52_evidence.os.read
            body_streams = 0
            requested_sizes: list[int] = []

            def tracking_stream(*args: object, **kwargs: object):
                nonlocal body_streams
                label = args[2] if len(args) > 2 else kwargs.get("label")
                if label == "quarantined response body":
                    body_streams += 1
                return original_stream(*args, **kwargs)  # type: ignore[arg-type]

            def tracking_read(descriptor: int, size: int) -> bytes:
                requested_sizes.append(size)
                return original_read(descriptor, size)

            with (
                patch.object(
                    v52_evidence,
                    "_read_streamed_regular_snapshot_at",
                    side_effect=tracking_stream,
                ),
                patch.object(v52_evidence.os, "read", side_effect=tracking_read),
            ):
                progress = self._read_progress(store, query)

            self.assertEqual(progress.status, "QUARANTINED")
            self.assertEqual(len(progress.quarantines), 2)
            self.assertEqual(body_streams, 2)
            self.assertTrue(requested_sizes)
            self.assertLessEqual(max(requested_sizes), 1024 * 1024)

    def test_verified_query_progress_rejects_declared_body_over_source_cap_before_stream(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                receipt_path = store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"bounded"),
                    error_classification="SEMANTIC_INVALID",
                )
            attempt_path = (
                root
                / "queries"
                / query.source
                / logical_query_id(query)
                / "attempts/000001.json"
            )
            for path in (receipt_path, attempt_path):
                receipt = json.loads(path.read_bytes())
                receipt["byte_count"] = 134_217_729
                path.write_bytes(_canonical_file(receipt))

            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "response byte limit|body size limit|cap"
            ):
                self._read_progress(store, query)

    def test_verified_query_progress_caps_match_frozen_acquisition_contract(
        self,
    ) -> None:
        contract = json.loads(
            (
                ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
            ).read_bytes()
        )
        self.assertEqual(
            v52_evidence._RESPONSE_BYTE_LIMITS,
            contract["response_byte_limits"],
        )
        cases = (
            (_query(), "schedule"),
            (
                dataclasses.replace(
                    _query(),
                    source="mlb_schedule_game_feed",
                    endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
                ),
                "boxscore",
            ),
            (
                dataclasses.replace(
                    _query(),
                    source="baseball_savant_statcast",
                    endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
                ),
                "statcast",
            ),
            (
                dataclasses.replace(
                    _query(),
                    source="mlb_stats_pitching",
                    endpoint="https://statsapi.mlb.com/api/v1/stats",
                ),
                "league_pitching",
            ),
            (
                dataclasses.replace(
                    _query(),
                    source="mlb_stats_pitcher_bf",
                    endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
                ),
                "career_bf",
            ),
            (
                dataclasses.replace(
                    _query(),
                    source="venue_team_timezone_table",
                    endpoint="https://statsapi.mlb.com/api/v1/teams",
                ),
                "team",
            ),
            (
                dataclasses.replace(
                    _query(),
                    source="venue_team_timezone_table",
                    endpoint="https://statsapi.mlb.com/api/v1/venues/10",
                ),
                "venue",
            ),
        )
        for query, key in cases:
            with self.subTest(key=key):
                limit = contract["response_byte_limits"][key]
                self.assertEqual(
                    v52_evidence._response_byte_limit(query),
                    limit,
                )
                self.assertEqual(
                    v52_evidence._validate_query_byte_count(query, limit),
                    limit,
                )
                with self.assertRaisesRegex(
                    ValueError, "source response byte limit"
                ):
                    v52_evidence._validate_query_byte_count(query, limit + 1)

    def test_verified_query_progress_rejects_declared_quarantine_aggregate_cap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                for number in range(1, 8):
                    store.quarantine_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(f"body-{number}".encode("ascii")),
                        error_classification="SEMANTIC_INVALID",
                    )
            query_id = logical_query_id(query)
            attempts = root / "queries" / query.source / query_id / "attempts"
            quarantine = root / "quarantine" / query.source / query_id
            for number in range(1, 8):
                for path in (
                    attempts / f"{number:06d}.json",
                    quarantine / f"{number:06d}.json",
                ):
                    receipt = json.loads(path.read_bytes())
                    receipt["byte_count"] = 134_217_728
                    path.write_bytes(_canonical_file(receipt))

            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "aggregate response byte limit"
            ):
                self._read_progress(store, query)

    def test_attempt_global_cap_is_shared_by_verifier_and_all_writers(self) -> None:
        valid = v52_evidence._attempt_record(
            logical_query_id(_query()),
            1,
            _capture(b"four"),
            "SUCCESS",
        )
        valid["byte_count"] = 268_435_457
        with self.assertRaisesRegex(ValueError, "response byte limit|maximum"):
            v52_evidence._validate_attempt_record(
                valid,
                logical_query_id(_query()),
                1,
            )

        operations = ("record", "publish")
        for operation in operations:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    attempt = store.next_attempt_number(lease)
                    with (
                        patch.object(
                            v52_evidence, "_MAX_RESPONSE_BYTE_COUNT", 3
                        ),
                        self.assertRaisesRegex(
                            ValueError, "response byte limit|maximum"
                        ),
                    ):
                        if operation == "record":
                            store.record_attempt(
                                query,
                                attempt,
                                _capture(b"four", status=500, error="HTTP_500"),
                                outcome="TRANSIENT_HTTP",
                            )
                        else:
                            store.publish_fetched(
                                query,
                                attempt,
                                _capture(b"four"),
                                _sidecar(),
                            )
                query_root = root / "queries" / query.source / logical_query_id(query)
                self.assertFalse((query_root / "attempts/000001.json").exists())
                self.assertEqual(list((query_root / "objects").iterdir()), [])
                self.assertFalse((query_root / "success.json").exists())

    def test_source_cap_is_shared_by_attempt_writers_before_artifacts(self) -> None:
        for operation in ("record", "publish"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    attempt = store.next_attempt_number(lease)
                    with (
                        patch.object(
                            v52_evidence, "_response_byte_limit", return_value=3
                        ),
                        self.assertRaisesRegex(
                            ValueError, "source response byte limit"
                        ),
                    ):
                        if operation == "record":
                            store.record_attempt(
                                query,
                                attempt,
                                _capture(b"four", status=500, error="HTTP_500"),
                                outcome="TRANSIENT_HTTP",
                            )
                        else:
                            store.publish_fetched(
                                query,
                                attempt,
                                _capture(b"four"),
                                _sidecar(),
                            )
                query_root = root / "queries" / query.source / logical_query_id(query)
                self.assertFalse((query_root / "attempts/000001.json").exists())
                self.assertEqual(list((query_root / "objects").iterdir()), [])
                self.assertEqual(list((query_root / "sidecars").iterdir()), [])
                self.assertFalse((query_root / "success.json").exists())

    def test_verified_readers_reject_source_oversized_attempt_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"four", status=500, error="HTTP_500"),
                    outcome="TRANSIENT_HTTP",
                )
            with (
                patch.object(v52_evidence, "_response_byte_limit", return_value=3),
                self.assertRaisesRegex(ValueError, "source response byte limit"),
            ):
                self._read_progress(store, query)

        for reader in ("success", "progress"):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as td:
                store = V52EvidenceStore(_tmp(td) / "raw")
                query = _query()
                with store.acquire_query(query) as lease:
                    store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"four"),
                        _sidecar(),
                    )
                with (
                    patch.object(
                        v52_evidence, "_response_byte_limit", return_value=3
                    ),
                    self.assertRaisesRegex(ValueError, "source response byte limit"),
                ):
                    if reader == "success":
                        store.read_verified_success(query)
                    else:
                        self._read_progress(store, query)

    def test_attempt_writers_reject_oversized_existing_history_before_artifacts(
        self,
    ) -> None:
        for operation in ("record", "publish"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    store.record_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"four", status=500, error="HTTP_500"),
                        outcome="TRANSIENT_HTTP",
                    )

                with (
                    patch.object(
                        v52_evidence, "_response_byte_limit", return_value=3
                    ),
                    store.acquire_query(query) as lease,
                ):
                    attempt = store.next_attempt_number(lease)
                    with self.assertRaisesRegex(
                        ValueError, "source response byte limit"
                    ):
                        if operation == "record":
                            store.record_attempt(
                                query,
                                attempt,
                                _capture(None, status=None, error="TIMEOUT"),
                                outcome="TRANSIENT_NETWORK",
                            )
                        else:
                            store.publish_fetched(
                                query,
                                attempt,
                                _capture(b"ok"),
                                _sidecar(),
                            )

                query_root = root / "queries" / query.source / logical_query_id(query)
                attempts = query_root / "attempts"
                self.assertTrue((attempts / "000002.reservation.json").exists())
                self.assertFalse((attempts / "000002.json").exists())
                self.assertEqual(list((query_root / "objects").iterdir()), [])
                self.assertEqual(list((query_root / "sidecars").iterdir()), [])
                self.assertFalse((query_root / "success.json").exists())

    def test_quarantine_writer_rejects_projected_aggregate_before_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with patch.object(v52_evidence, "_response_byte_limit", return_value=4):
                with store.acquire_query(query) as lease:
                    bodies = (b"aaaa", b"aaaa", b"bbbb", b"cccc", b"dddd", b"eeee", b"ffff")
                    for body in bodies:
                        store.quarantine_attempt(
                            query,
                            store.next_attempt_number(lease),
                            _capture(body),
                            error_classification="SEMANTIC_INVALID",
                        )
                    rejected_attempt = store.next_attempt_number(lease)
                    rejected_body = b"gggg"
                    with self.assertRaisesRegex(
                        ValueError, "aggregate response byte limit|projected"
                    ):
                        store.quarantine_attempt(
                            query,
                            rejected_attempt,
                            _capture(rejected_body),
                            error_classification="SEMANTIC_INVALID",
                        )

            query_id = logical_query_id(query)
            attempts = root / "queries" / query.source / query_id / "attempts"
            quarantine = root / "quarantine" / query.source / query_id
            rejected_digest = hashlib.sha256(rejected_body).hexdigest()
            self.assertTrue(
                (attempts / f"{rejected_attempt:06d}.reservation.json").exists()
            )
            self.assertFalse((attempts / f"{rejected_attempt:06d}.json").exists())
            self.assertFalse((quarantine / f"{rejected_attempt:06d}.json").exists())
            self.assertFalse((quarantine / f"{rejected_digest}.raw").exists())
            self.assertEqual(
                len(list(quarantine.glob("*.raw"))),
                6,
                "the duplicate digest must not count twice",
            )

    def test_quarantine_duplicate_digest_reuses_streaming_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            body = b"same quarantined body"
            original_read = v52_evidence._read_regular_at

            def reject_duplicate_whole_file_read(
                parent_fd: int,
                name: str,
                label: str,
                *args: object,
                **kwargs: object,
            ) -> bytes | None:
                if label == "quarantined response body":
                    raise AssertionError(
                        "verified duplicate raw body must not be read whole again"
                    )
                return original_read(
                    parent_fd,
                    name,
                    label,
                    *args,
                    **kwargs,
                )

            with store.acquire_query(query) as lease:
                store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(body),
                    error_classification="SEMANTIC_INVALID",
                )
                with patch.object(
                    v52_evidence,
                    "_read_regular_at",
                    side_effect=reject_duplicate_whole_file_read,
                ):
                    store.quarantine_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(body),
                        error_classification="SEMANTIC_INVALID",
                    )

    def test_verified_query_progress_rejects_mixed_canonical_root_generations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = _tmp(td)
            first_root = base / "first"
            second_root = base / "second"
            store = V52EvidenceStore(first_root)
            query = _query()
            with store.acquire_query(query) as lease:
                store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"invalid-but-owned"),
                    error_classification="SEMANTIC_INVALID",
                )
            shutil.copytree(first_root, second_root)
            roots = (first_root, second_root, second_root, first_root)
            calls = 0

            def alternating_root() -> int:
                nonlocal calls
                selected = roots[calls] if calls < len(roots) else first_root
                calls += 1
                return v52_evidence._open_root_path(selected, create=False)

            with (
                patch.object(store, "_root_fd", side_effect=alternating_root),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "root|generation|stitch|identity"
                ),
            ):
                self._read_progress(store, query)

    def test_verified_query_progress_rejects_quarantine_receipt_and_body_tamper(
        self,
    ) -> None:
        mutations = ("path", "attempt", "body")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    receipt_path = store.quarantine_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"invalid-but-owned"),
                        error_classification="SEMANTIC_INVALID",
                    )
                receipt = json.loads(receipt_path.read_bytes())
                raw_path = root / receipt["quarantined_body_path"]
                if mutation == "path":
                    receipt["quarantined_body_path"] = (
                        f"quarantine/{query.source}/{logical_query_id(query)}/"
                        + "0" * 64
                        + ".raw"
                    )
                    receipt_path.write_bytes(_canonical_file(receipt))
                elif mutation == "attempt":
                    receipt["error_classification"] = "DIFFERENT"
                    receipt_path.write_bytes(_canonical_file(receipt))
                else:
                    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")

                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "quarantine|quarantined|body|attempt|hash|binding|path|identity",
                ):
                    self._read_progress(store, query)

    def test_verified_query_progress_rejects_orphan_missing_and_aliased_quarantine(
        self,
    ) -> None:
        mutations = (
            "orphan-without-query",
            "orphan-directory",
            "missing-receipt",
            "missing-body",
            "extra-member",
            "symlink",
            "hardlink",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                query_id = logical_query_id(query)
                if mutation in {"orphan-without-query", "orphan-directory"}:
                    if mutation == "orphan-directory":
                        store.register_query(query)
                    (root / "quarantine" / query.source / query_id).mkdir(parents=True)
                else:
                    with store.acquire_query(query) as lease:
                        receipt_path = store.quarantine_attempt(
                            query,
                            store.next_attempt_number(lease),
                            _capture(b"invalid-but-owned"),
                            error_classification="SEMANTIC_INVALID",
                        )
                    receipt = json.loads(receipt_path.read_bytes())
                    raw_path = root / receipt["quarantined_body_path"]
                    if mutation == "missing-receipt":
                        receipt_path.unlink()
                    elif mutation == "missing-body":
                        raw_path.unlink()
                    elif mutation == "extra-member":
                        (receipt_path.parent / "unexpected.raw").write_bytes(b"orphan")
                    elif mutation == "symlink":
                        outside = root / "outside.raw"
                        outside.write_bytes(raw_path.read_bytes())
                        raw_path.unlink()
                        raw_path.symlink_to(outside)
                    else:
                        os.link(raw_path, root / "hardlink-alias.raw")

                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "quarantine|quarantined|missing|ambiguous|regular|membership|alias",
                ):
                    self._read_progress(store, query)

    def test_verified_query_progress_shared_lock_hides_inflight_writer(self) -> None:
        self.assertTrue(hasattr(V52EvidenceStore, "read_verified_query_progress"))
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            store.register_query(query)
            entered = threading.Event()
            release = threading.Event()
            reader_done = threading.Event()
            result: list[object] = []

            def writer() -> None:
                with store.acquire_query(query):
                    entered.set()
                    release.wait(5)

            def reader() -> None:
                result.append(self._read_progress(store, query))
                reader_done.set()

            writer_thread = threading.Thread(target=writer)
            reader_thread = threading.Thread(target=reader)
            writer_thread.start()
            self.assertTrue(entered.wait(2))
            reader_thread.start()
            self.assertFalse(reader_done.wait(0.1))
            release.set()
            writer_thread.join(2)
            reader_thread.join(2)
            self.assertFalse(writer_thread.is_alive())
            self.assertFalse(reader_thread.is_alive())
            self.assertEqual(result[0].status, "PENDING")

    def test_verified_query_progress_shared_lock_blocks_writer_until_read_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            store.register_query(query)
            reader_holds_lock = threading.Event()
            release_reader = threading.Event()
            writer_entered = threading.Event()
            thread_errors: list[BaseException] = []
            original = store._verified_attempt_bundle
            blocked = False

            def block_reader(*args: object, **kwargs: object):
                nonlocal blocked
                result = original(*args, **kwargs)  # type: ignore[arg-type]
                if not blocked:
                    blocked = True
                    reader_holds_lock.set()
                    release_reader.wait(5)
                return result

            def reader() -> None:
                try:
                    self._read_progress(store, query)
                except BaseException as exc:
                    thread_errors.append(exc)

            def writer() -> None:
                try:
                    with store.acquire_query(query):
                        writer_entered.set()
                except BaseException as exc:
                    thread_errors.append(exc)

            with patch.object(
                store, "_verified_attempt_bundle", side_effect=block_reader
            ):
                reader_thread = threading.Thread(target=reader)
                writer_thread = threading.Thread(target=writer)
                reader_thread.start()
                self.assertTrue(reader_holds_lock.wait(2))
                writer_thread.start()
                self.assertFalse(writer_entered.wait(0.1))
                release_reader.set()
                reader_thread.join(2)
                writer_thread.join(2)
            self.assertFalse(reader_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertTrue(writer_entered.is_set())
            self.assertEqual(thread_errors, [])

    def test_verified_query_progress_repeated_exceptions_do_not_leak_descriptors(
        self,
    ) -> None:
        descriptor_directory = Path("/dev/fd")
        if not descriptor_directory.is_dir():
            self.skipTest("descriptor inventory is unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                receipt_path = store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"invalid-but-owned"),
                    error_classification="SEMANTIC_INVALID",
                )
            receipt = json.loads(receipt_path.read_bytes())
            raw_path = root / receipt["quarantined_body_path"]
            raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
            before = len(list(descriptor_directory.iterdir()))
            for _ in range(20):
                with self.assertRaises((ValueError, RuntimeError)):
                    self._read_progress(store, query)
            after = len(list(descriptor_directory.iterdir()))
            self.assertEqual(after, before)

    def test_verified_query_progress_rejects_quarantine_mutation_during_rebind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                receipt_path = store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"invalid-but-owned"),
                    error_classification="SEMANTIC_INVALID",
                )
            receipt = json.loads(receipt_path.read_bytes())
            raw_path = root / receipt["quarantined_body_path"]
            original = v52_evidence._read_streamed_regular_snapshot_at
            body_passes = 0

            def mutate_before_final_stream(*args: object, **kwargs: object):
                nonlocal body_passes
                label = args[2] if len(args) > 2 else kwargs.get("label")
                if label == "quarantined response body":
                    body_passes += 1
                if body_passes == 2:
                    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(
                    v52_evidence,
                    "_read_streamed_regular_snapshot_at",
                    side_effect=mutate_before_final_stream,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "quarantine|body|identity|changed"
                ),
            ):
                self._read_progress(store, query)

    def test_quarantine_receipt_schema_is_closed_and_branch_specific(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                receipt_path = store.quarantine_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"invalid-but-owned"),
                    error_classification="SEMANTIC_INVALID",
                )
            schema = json.loads(MANIFEST_SCHEMA.read_bytes())
            receipt = json.loads(receipt_path.read_bytes())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(receipt, schema)
            receipt["unknown"] = "forbidden"
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(receipt, schema)
            oversized = json.loads(receipt_path.read_bytes())
            oversized["byte_count"] = 268_435_457
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(oversized, schema)

    def test_evidence_manifest_schema_uses_canonical_sorted_json_bytes(self) -> None:
        contents = MANIFEST_SCHEMA.read_bytes()
        self.assertEqual(contents, _canonical_file(json.loads(contents)))

    def test_verified_read_returns_one_fully_bound_fetched_and_legacy_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            capture = _capture(b'{"dates":[{"date":"2022-04-07"}]}')
            with store.acquire_query(query) as lease:
                failed_number = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    failed_number,
                    _capture(None, status=None, error="TIMEOUT"),
                    outcome="TRANSIENT_NETWORK",
                )
                published = store.publish_fetched(
                    query, store.next_attempt_number(lease), capture, _sidecar()
                )
            verified = store.read_verified_success(query)
            self.assertEqual(verified.registered_query, query)
            self.assertEqual(verified.published_evidence, published)
            self.assertEqual(verified.raw_bytes, capture.body)
            self.assertEqual(verified.query_sidecar["semantics"], _sidecar())
            self.assertEqual(verified.success_receipt["storage_mode"], "FETCHED_RAW")
            self.assertEqual(
                [record["attempt_number"] for record in verified.attempt_receipts],
                [1, 2],
            )
            self.assertEqual(
                [record["outcome"] for record in verified.attempt_receipts],
                ["TRANSIENT_NETWORK", "SUCCESS"],
            )

        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            with store.acquire_query(query):
                published = store.publish_legacy_reference(
                    query, reference, _sidecar()
                )
            verified = store.read_verified_success(query)
            self.assertEqual(verified.published_evidence, published)
            self.assertEqual(verified.raw_bytes, b"legacy-one\nlegacy-two\n")
            self.assertEqual(verified.attempt_receipts, ())

    def test_load_success_uses_one_verified_tree_open_without_preflight_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(),
                    _sidecar(),
                )
            original = store._query_fds
            calls = 0

            def count_opens(*args: object, **kwargs: object):
                nonlocal calls
                calls += 1
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(store, "_query_fds", side_effect=count_opens):
                self.assertEqual(store.load_success(query), published)
            self.assertEqual(calls, 1)

    def test_verified_read_rejects_attempt_tail_added_after_initial_enumeration(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(),
                    _sidecar(),
                )
            query_id = logical_query_id(query)
            attempts = root / "queries" / query.source / query_id / "attempts"
            original = store._verified_attempt_bundle
            injected = False

            def inject_tail(*args: object, **kwargs: object):
                nonlocal injected
                result = original(*args, **kwargs)  # type: ignore[arg-type]
                if not injected:
                    injected = True
                    number = 2
                    reservation = {
                        "attempt_number": number,
                        "query_id": query_id,
                        "schema_version": 1,
                    }
                    receipt = v52_evidence._attempt_record(
                        query_id,
                        number,
                        _capture(None, status=None, error="TIMEOUT"),
                        "TRANSIENT_NETWORK",
                    )
                    (attempts / "000002.reservation.json").write_bytes(
                        _canonical_file(reservation)
                    )
                    (attempts / "000002.json").write_bytes(
                        _canonical_file(receipt)
                    )
                return result

            with (
                patch.object(
                    store,
                    "_verified_attempt_bundle",
                    side_effect=inject_tail,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "attempt|history|changed|tail"
                ),
            ):
                store.read_verified_success(query)

    def test_verified_read_rejects_success_mutation_during_final_attempt_rebind(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(),
                    _sidecar(),
                )
            original = store._verified_attempt_bundle
            calls = 0

            def mutate_success(*args: object, **kwargs: object):
                nonlocal calls
                calls += 1
                if calls == 2:
                    published.success_receipt_path.write_bytes(
                        published.success_receipt_path.read_bytes() + b" "
                    )
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(
                    store,
                    "_verified_attempt_bundle",
                    side_effect=mutate_success,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError), "success|identity|changed|canonical"
                ),
            ):
                store.read_verified_success(query)

    def test_derived_disposition_has_no_raw_or_attempt_and_is_retry_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _derived_query()
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                with store.acquire_query(query):
                    with patch.object(
                        store,
                        "_verify_query_chain",
                        side_effect=RuntimeError("derived interruption"),
                    ):
                        store.publish_derived_disposition(
                            query, _derived_semantics()
                        )
            folder = root / "queries" / query.source / logical_query_id(query)
            self.assertFalse((folder / "success.json").exists())
            self.assertEqual(list((folder / "objects").iterdir()), [])
            self.assertEqual(list((folder / "attempts").iterdir()), [])
            self.assertEqual(len(list((folder / "sidecars").iterdir())), 1)
            with store.acquire_query(query):
                published = store.publish_derived_disposition(
                    query, _derived_semantics()
                )
            self.assertEqual(published.storage_mode, "DERIVED_DISPOSITION")
            self.assertIsNone(published.raw_sha256)
            verified = store.read_verified_success(query)
            self.assertIsNone(verified.raw_bytes)
            self.assertEqual(verified.attempt_receipts, ())
            self.assertEqual(verified.success_receipt["attempt_history"], [])
            self.assertIsNone(verified.success_receipt["attempt_number"])
            self.assertNotIn("raw_sha256", verified.success_receipt)
            self.assertNotIn("raw_byte_count", verified.success_receipt)
            manifest_schema = json.loads(MANIFEST_SCHEMA.read_bytes())
            derived_schema = json.loads(DERIVED_SIDECAR_SCHEMA.read_bytes())
            jsonschema.Draft202012Validator.check_schema(manifest_schema)
            jsonschema.Draft202012Validator.check_schema(derived_schema)
            jsonschema.validate(verified.success_receipt, manifest_schema)
            jsonschema.validate(verified.query_sidecar, derived_schema)

    def test_derived_publisher_rejects_hook_sidecar_corruption_before_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _derived_query()
            query_id = logical_query_id(query)
            sidecars = root / "queries" / query.source / query_id / "sidecars"
            original_verify = store._verify_query_chain

            def corrupt_sidecar_then_verify(
                *args: object, **kwargs: object
            ) -> None:
                paths = list(sidecars.iterdir())
                self.assertEqual(len(paths), 1)
                paths[0].write_bytes(paths[0].read_bytes() + b" ")
                original_verify(*args, **kwargs)  # type: ignore[arg-type]

            with store.acquire_query(query):
                with (
                    patch.object(
                        store,
                        "_verify_query_chain",
                        side_effect=corrupt_sidecar_then_verify,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "sidecar|identity|changed|canonical|content",
                    ),
                ):
                    store.publish_derived_disposition(
                        query, _derived_semantics()
                    )
            folder = root / "queries" / query.source / query_id
            self.assertFalse((folder / "success.json").exists())
            self.assertEqual(list(sidecars.iterdir()), [])
            with store.acquire_query(query):
                published = store.publish_derived_disposition(
                    query, _derived_semantics()
                )
            self.assertEqual(
                store.read_verified_success(query).published_evidence,
                published,
            )

    def test_derived_disposition_rejects_query_semantic_attempt_and_content_tamper(self):
        query_mutations = (
            _derived_query(endpoint="https://statsapi.mlb.com/api/v1/stats"),
            _derived_query(parameters={"limit": 1}),
            _derived_query(expected_media_type="text/plain"),
            _derived_query(
                requested_range={
                    **_derived_query().requested_range,
                    "as_of_date": "2024-03-20",
                    "consumer_cutoff_dates": {"9001": "2024-03-20"},
                }
            ),
            _derived_query(
                requested_range={
                    **_derived_query().requested_range,
                    "reason": "MADE_UP",
                }
            ),
        )
        for query in query_mutations:
            with self.subTest(query=query), tempfile.TemporaryDirectory() as td:
                store = V52EvidenceStore(_tmp(td) / "raw")
                with store.acquire_query(query):
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.publish_derived_disposition(
                            query, _derived_semantics()
                        )

        for attack in ("semantics", "attempt", "reservation", "object"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _derived_query()
                if attack == "semantics":
                    semantics = {**_derived_semantics(), "reason": "MADE_UP"}
                    with store.acquire_query(query):
                        with self.assertRaises((ValueError, RuntimeError)):
                            store.publish_derived_disposition(query, semantics)
                    continue
                if attack == "reservation":
                    with store.acquire_query(query) as lease:
                        store.next_attempt_number(lease)
                        with self.assertRaises((ValueError, RuntimeError)):
                            store.publish_derived_disposition(
                                query, _derived_semantics()
                            )
                    continue
                store.register_query(query)
                folder = root / "queries" / query.source / logical_query_id(query)
                target = folder / ("attempts" if attack == "attempt" else "objects")
                (target / "unexpected.json").write_bytes(b"{}\n")
                with store.acquire_query(query):
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.publish_derived_disposition(
                            query, _derived_semantics()
                        )

        for attack in ("sidecar", "attempt", "object"):
            with self.subTest(post_publish=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _derived_query()
                with store.acquire_query(query):
                    published = store.publish_derived_disposition(
                        query, _derived_semantics()
                    )
                folder = root / "queries" / query.source / logical_query_id(query)
                success = json.loads(published.success_receipt_path.read_bytes())
                if attack == "sidecar":
                    (root / success["query_sidecar_path"]).write_bytes(b"{}\n")
                else:
                    target = folder / (
                        "attempts" if attack == "attempt" else "objects"
                    )
                    (target / "unexpected.json").write_bytes(b"{}\n")
                with self.assertRaises((ValueError, RuntimeError)):
                    store.read_verified_success(query)

    def test_derived_disposition_requires_exact_frozen_season_start_d1(self):
        too_early_range = {
            **_derived_query().requested_range,
            "as_of_date": "2024-03-18",
            "consumer_cutoff_dates": {"9001": "2024-03-18"},
        }
        too_early_semantics = {
            **_derived_semantics(),
            "as_of_date": "2024-03-18",
            "consumer_cutoff_dates": {"9001": "2024-03-18"},
        }
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _derived_query(requested_range=too_early_range)
            with store.acquire_query(query):
                with self.assertRaisesRegex(ValueError, "D-1|season start|date"):
                    store.publish_derived_disposition(
                        query, too_early_semantics
                    )

    def test_canonical_parameter_ordering_and_identity_field_sensitivity(self) -> None:
        first = _query(
            parameters={"z": [3, 2, 1], "a": {"two": 2, "one": 1}},
            requested_range={
                "inclusive_end": "2022-04-08",
                "inclusive_start": "2022-04-07",
            },
        )
        reordered = _query(
            parameters={"a": {"one": 1, "two": 2}, "z": [3, 2, 1]},
            requested_range={
                "inclusive_start": "2022-04-07",
                "inclusive_end": "2022-04-08",
            },
        )
        self.assertEqual(canonical_query_bytes(first), canonical_query_bytes(reordered))
        self.assertEqual(logical_query_id(first), logical_query_id(reordered))
        self.assertFalse(canonical_query_bytes(first).endswith(b"\n"))
        self.assertEqual(
            logical_query_id(first),
            hashlib.sha256(canonical_query_bytes(first)).hexdigest(),
        )

        variants = (
            _query(source="baseball_savant"),
            _query(grain="statcast_pitch"),
            _query(endpoint="https://example.test/other"),
            _query(parameters={"sportId": 2, "hydrate": "venue,lineups"}),
            _query(
                requested_range={
                    "inclusive_start": "2022-04-07",
                    "inclusive_end": "2022-04-09",
                }
            ),
            _query(expected_media_type="text/csv"),
            _query(parser_version="schedule-v2"),
            _query(parent_query_id="c" * 64),
            _query(run_spec_sha256="d" * 64),
            _query(acquisition_contract_sha256="e" * 64),
        )
        baseline = logical_query_id(_query())
        self.assertEqual(
            len({baseline, *(logical_query_id(item) for item in variants)}), 11
        )

    def test_register_query_uses_required_layout_and_rejects_duplicate_ambiguity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            store = V52EvidenceStore(root)
            query_id = store.register_query(query)
            folder = root / "queries" / query.source / query_id
            registration = json.loads((folder / "query.json").read_bytes())
            registration.pop("lock_identity")
            self.assertEqual(
                registration,
                json.loads(canonical_query_bytes(query)),
            )
            for child in ("attempts", "objects", "sidecars"):
                self.assertTrue((folder / child).is_dir())
            self.assertEqual(store.register_query(query), query_id)

            (folder / "query.json").write_bytes(
                canonical_query_bytes(_query(grain="other")) + b"\n"
            )
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "query|ambiguous|differ"
            ):
                store.register_query(query)

    def test_register_query_rebinds_registration_and_query_generation_before_return(
        self,
    ) -> None:
        for attack in ("replace", "symlink", "generation"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                query_id = store.register_query(query)
                query_root = root / "queries" / query.source / query_id
                registration = query_root / "query.json"
                original = registration.read_bytes()
                original_validate = store._validated_registered_query
                attacked = False

                def validate_then_attack(*args: object, **kwargs: object):
                    nonlocal attacked
                    result = original_validate(*args, **kwargs)
                    if attacked:
                        return result
                    attacked = True
                    if attack == "replace":
                        replacement = query_root / ".replacement.json"
                        replacement.write_bytes(original)
                        os.replace(replacement, registration)
                    elif attack == "symlink":
                        target = _tmp(td) / "same-registration.json"
                        target.write_bytes(original)
                        registration.unlink()
                        registration.symlink_to(target)
                    else:
                        detached = query_root.with_name(query_root.name + ".detached")
                        query_root.rename(detached)
                        shutil.copytree(detached, query_root)
                    return result

                with (
                    patch.object(
                        store,
                        "_validated_registered_query",
                        side_effect=validate_then_attack,
                    ),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    store.register_query(query)

    def test_register_query_registration_snapshot_rebind_is_last_locked_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            query_id = store.register_query(query)
            query_path = root / "queries" / query.source / query_id / "query.json"
            original = query_path.read_bytes()
            original_chain = store._verify_query_chain
            attacked = False

            def chain_then_replace(*args: object, **kwargs: object) -> None:
                nonlocal attacked
                original_chain(*args, **kwargs)
                if not attacked:
                    attacked = True
                    replacement = query_path.with_name(".same-query.json")
                    replacement.write_bytes(original)
                    os.replace(replacement, query_path)

            with (
                patch.object(store, "_verify_query_chain", side_effect=chain_then_replace),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                store.register_query(query)

    def test_query_registration_immutably_binds_lock_device_and_inode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            store = V52EvidenceStore(root)
            query_id = store.register_query(query)
            folder = root / "queries" / query.source / query_id
            registration = json.loads((folder / "query.json").read_bytes())
            lock_metadata = (folder / ".query.lock").lstat()
            logical_record = json.loads(canonical_query_bytes(query))
            self.assertEqual(
                registration,
                {
                    **logical_record,
                    "lock_identity": {
                        "device": lock_metadata.st_dev,
                        "inode": lock_metadata.st_ino,
                    },
                },
            )

    def test_concurrent_first_registration_never_observes_partial_query_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            partial_created = threading.Event()
            allow_completion = threading.Event()
            results: list[str] = []
            errors: list[BaseException] = []
            original_write = os.write
            paused = False
            pause_lock = threading.Lock()

            def pause_first_registration(
                descriptor: int, contents: bytes | bytearray | memoryview
            ) -> int:
                nonlocal paused
                with pause_lock:
                    should_pause = not paused
                    if should_pause:
                        paused = True
                if should_pause:
                    partial_created.set()
                    self.assertTrue(allow_completion.wait(timeout=5))
                return original_write(descriptor, contents)

            def worker() -> None:
                try:
                    results.append(V52EvidenceStore(root).register_query(query))
                except BaseException as exc:
                    errors.append(exc)

            with patch(
                "mlb_backtest.v52_evidence.os.write",
                side_effect=pause_first_registration,
            ):
                first = threading.Thread(target=worker)
                second = threading.Thread(target=worker)
                first.start()
                self.assertTrue(partial_created.wait(timeout=5))
                second.start()
                time.sleep(0.1)
                self.assertEqual(results, [])
                self.assertEqual(errors, [])
                self.assertTrue(second.is_alive())
                allow_completion.set()
                first.join(timeout=5)
                second.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(results, [logical_query_id(query)] * 2)
            folder = root / "queries" / query.source / logical_query_id(query)
            registration = (folder / "query.json").read_bytes()
            self.assertEqual(registration, _canonical_file(json.loads(registration)))
            self.assertEqual(len(list(folder.glob("query.json"))), 1)

    def test_nested_same_thread_lease_fails_before_registration_and_outer_survives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            other_query = _query(grain="other-grain")
            with store.acquire_query(query) as outer_lease:
                with patch.object(
                    store,
                    "register_query",
                    side_effect=AssertionError(
                        "nested acquire must fail before registration"
                    ),
                ):
                    for nested_query in (query, other_query):
                        with self.subTest(query_id=logical_query_id(nested_query)):
                            with self.assertRaisesRegex(
                                RuntimeError, "lease|active|reentrant"
                            ):
                                with store.acquire_query(nested_query):
                                    self.fail("nested lease unexpectedly entered")
                self.assertEqual(store.next_attempt_number(outer_lease), 1)
            with store.acquire_query(query) as reopened_lease:
                self.assertEqual(store.next_attempt_number(reopened_lease), 2)

    def test_directory_open_helpers_close_child_fd_on_identity_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = _tmp(td)
            child = directory / "child"
            child.mkdir()
            real_open = os.open
            for helper in ("root", "child"):
                with self.subTest(helper=helper):
                    opened: list[int] = []

                    def track_open(*args: object, **kwargs: object) -> int:
                        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
                        opened.append(descriptor)
                        return descriptor

                    if helper == "root":
                        with (
                            patch.object(
                                v52_evidence.os, "open", side_effect=track_open
                            ),
                            patch.object(
                                v52_evidence.os,
                                "stat",
                                side_effect=OSError("injected identity failure"),
                            ),
                        ):
                            for _ in range(5):
                                with self.assertRaisesRegex(OSError, "injected"):
                                    v52_evidence._open_root_path(child, create=False)
                    else:
                        parent_fd = real_open(directory, os.O_RDONLY)
                        try:
                            with (
                                patch.object(
                                    v52_evidence.os, "open", side_effect=track_open
                                ),
                                patch.object(
                                    v52_evidence.os,
                                    "stat",
                                    side_effect=OSError("injected identity failure"),
                                ),
                            ):
                                for _ in range(5):
                                    with self.assertRaisesRegex(OSError, "injected"):
                                        v52_evidence._open_directory_at(
                                            parent_fd, "child", create=False
                                        )
                        finally:
                            os.close(parent_fd)
                    self.assertTrue(opened)
                    for descriptor in set(opened):
                        with self.assertRaises(OSError):
                            os.fstat(descriptor)

    def test_lock_bound_query_registration_rejects_tamper_and_aliases(self) -> None:
        for attack in ("tamper", "symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                query = _query()
                store = V52EvidenceStore(root)
                with store.acquire_query(query):
                    pass
                folder = root / "queries" / query.source / logical_query_id(query)
                registration_path = folder / "query.json"
                if attack == "tamper":
                    registration = json.loads(registration_path.read_bytes())
                    registration["lock_identity"]["inode"] += 1
                    registration_path.write_bytes(_canonical_file(registration))
                else:
                    displaced = folder / "query.displaced.json"
                    registration_path.rename(displaced)
                    if attack == "symlink":
                        registration_path.symlink_to(displaced.name)
                    else:
                        os.link(displaced, registration_path)
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "query|registration|lock|identity|alias|unalias|differ",
                ):
                    with V52EvidenceStore(root).acquire_query(query):
                        self.fail("tampered query registration was accepted")

    def test_append_only_attempt_numbering_selected_headers_and_exact_byte_count(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            capture = _capture(b"abc\x00def", status=503, error="TRANSIENT_HTTP")
            with store.acquire_query(query) as lease:
                first = store.next_attempt_number(lease)
                self.assertEqual(first, 1)
                receipt_path = store.record_attempt(
                    query, first, capture, outcome="TRANSIENT_HTTP"
                )
            receipt = json.loads(receipt_path.read_bytes())
            self.assertEqual(receipt["attempt_number"], 1)
            self.assertEqual(receipt["byte_count"], 7)
            self.assertNotIn("bytes_received", receipt)
            self.assertEqual(
                receipt["response_headers"],
                {
                    "Content-Length": "7",
                    "Content-Type": "application/json; charset=utf-8",
                    "ETag": '"fixture"',
                    "Last-Modified": "Tue, 14 Jul 2026 12:00:00 GMT",
                    "Retry-After": "3",
                },
            )
            self.assertNotIn("X-Ignored", receipt["response_headers"])
            self.assertEqual(receipt_path.read_bytes(), _canonical_file(receipt))
            schema = json.loads(MANIFEST_SCHEMA.read_bytes())
            jsonschema.validate(receipt, schema)

            with store.acquire_query(query) as lease:
                self.assertEqual(store.next_attempt_number(lease), 2)
                self.assertEqual(store.next_attempt_number(lease), 3)
            reservations = sorted(receipt_path.parent.glob("*.reservation.json"))
            self.assertEqual(
                [path.name for path in reservations],
                [
                    "000001.reservation.json",
                    "000002.reservation.json",
                    "000003.reservation.json",
                ],
            )

    def test_forged_or_incomplete_attempt_receipts_never_enter_history(self) -> None:
        mutations = {
            "missing": lambda item: item.pop("final_url"),
            "timestamp": lambda item: item.__setitem__(
                "started_at_utc", "2026-99-99T12:00:00.000001Z"
            ),
            "header": lambda item: item["response_headers"].__setitem__(
                "X-Unselected", "forbidden"
            ),
            "content_length": lambda item: item["response_headers"].__setitem__(
                "Content-Length", "-1"
            ),
            "byte_count": lambda item: item.__setitem__("byte_count", -1),
            "outcome": lambda item: item.__setitem__("outcome", "UNKNOWN"),
            "attempt_binding": lambda item: item.__setitem__("attempt_number", 2),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                receipt = {
                    "attempt_number": number,
                    "body_sha256": hashlib.sha256(b"failure").hexdigest(),
                    "byte_count": len(b"failure"),
                    "ended_at_utc": "2026-07-14T12:00:01.000001Z",
                    "error_classification": "TRANSIENT_HTTP",
                    "final_url": "https://statsapi.mlb.com/api/v1/schedule",
                    "http_status": 503,
                    "outcome": "TRANSIENT_HTTP",
                    "query_id": logical_query_id(query),
                    "response_headers": {
                        "Content-Length": str(len(b"failure")),
                        "Content-Type": "application/json",
                    },
                    "schema_version": 1,
                    "started_at_utc": "2026-07-14T12:00:00.000001Z",
                }
                mutate(receipt)
                path = (
                    root
                    / "queries"
                    / query.source
                    / logical_query_id(query)
                    / "attempts"
                    / "000001.json"
                )
                path.write_bytes(_canonical_file(receipt))
                with store.acquire_query(query) as lease:
                    with self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "attempt|timestamp|header|byte|outcome|keys|binding",
                    ):
                        store.next_attempt_number(lease)

    def test_content_length_is_independent_selected_response_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            body = b"short"
            capture = dataclasses.replace(
                _capture(body, status=503, error="TRUNCATED_HTTP"),
                response_headers=(
                    ("Content-Length", "100"),
                    ("Content-Type", "application/json"),
                ),
            )
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                receipt_path = store.record_attempt(
                    query, number, capture, outcome="TRANSIENT_HTTP"
                )
            receipt = json.loads(receipt_path.read_bytes())
            self.assertEqual(receipt["byte_count"], len(body))
            self.assertEqual(receipt["body_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(receipt["response_headers"]["Content-Length"], "100")
            jsonschema.validate(receipt, json.loads(MANIFEST_SCHEMA.read_bytes()))
            with store.acquire_query(query) as lease:
                self.assertEqual(store.next_attempt_number(lease), 2)

    def test_content_length_rejects_unicode_decimal_digits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            capture = dataclasses.replace(
                _capture(b"short", status=503, error="TRANSIENT_HTTP"),
                response_headers=(("Content-Length", "١٠٠"),),
            )
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                with self.assertRaisesRegex(ValueError, "Content-Length|syntax"):
                    store.record_attempt(
                        query, number, capture, outcome="TRANSIENT_HTTP"
                    )

    def test_attempt_schema_is_closed_and_enforces_branch_semantics(self) -> None:
        schema = json.loads(MANIFEST_SCHEMA.read_bytes())
        receipt = {
            "attempt_number": 1,
            "body_sha256": hashlib.sha256(b"ok").hexdigest(),
            "byte_count": 2,
            "ended_at_utc": "2026-07-14T12:00:01.000001Z",
            "error_classification": None,
            "final_url": "https://statsapi.mlb.com/api/v1/schedule",
            "http_status": 200,
            "outcome": "SUCCESS",
            "query_id": "a" * 64,
            "response_headers": {"Content-Type": "application/json"},
            "schema_version": 1,
            "started_at_utc": "2026-07-14T12:00:00.000001Z",
        }
        jsonschema.validate(receipt, schema)
        for mutation in (
            lambda item: item.__setitem__("unknown", True),
            lambda item: item.__setitem__("http_status", 503),
            lambda item: item.__setitem__("error_classification", "ERROR"),
            lambda item: item["response_headers"].__setitem__("X-Other", "bad"),
            lambda item: (
                item.__setitem__("body_sha256", None),
                item.__setitem__("byte_count", 2),
            ),
        ):
            invalid = json.loads(json.dumps(receipt))
            mutation(invalid)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)
        invalid_network = {
            **receipt,
            "body_sha256": None,
            "byte_count": 2,
            "error_classification": "TIMEOUT",
            "final_url": None,
            "http_status": None,
            "outcome": "TRANSIENT_NETWORK",
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid_network, schema)

    def test_retry_resume_never_reuses_an_interrupted_attempt_number(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                self.assertEqual(store.next_attempt_number(lease), 1)
            with store.acquire_query(query) as lease:
                second = store.next_attempt_number(lease)
                self.assertEqual(second, 2)
                store.record_attempt(
                    query,
                    second,
                    _capture(None, status=None, error="TIMEOUT"),
                    outcome="TRANSIENT_NETWORK",
                )
            with store.acquire_query(query) as lease:
                self.assertEqual(store.next_attempt_number(lease), 3)

    def test_quarantine_attempt_persists_invalid_body_outside_query_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            body = b"invalid-but-owned"
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                receipt_path = store.quarantine_attempt(
                    query,
                    number,
                    _capture(body),
                    error_classification="SEMANTIC_INVALID",
                )
            quarantine = root / "quarantine" / query.source / logical_query_id(query)
            self.assertEqual(receipt_path.parent, quarantine)
            receipt = json.loads(receipt_path.read_bytes())
            object_path = root / receipt["quarantined_body_path"]
            self.assertEqual(object_path.read_bytes(), body)
            self.assertEqual(receipt["raw_sha256"], hashlib.sha256(body).hexdigest())
            attempt = json.loads(
                (
                    root
                    / "queries"
                    / query.source
                    / logical_query_id(query)
                    / "attempts"
                    / "000001.json"
                ).read_bytes()
            )
            self.assertEqual(attempt["outcome"], "QUARANTINED")
            self.assertEqual(attempt["error_classification"], "SEMANTIC_INVALID")

    def test_bodyless_terminal_failure_is_canonical_checkpoint_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            capture = dataclasses.replace(
                _capture(None, status=None, error="PERMANENT_NETWORK"),
                final_url=None,
                response_headers=(),
            )
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                evidence = store.quarantine_bodyless_attempt(
                    query,
                    number,
                    capture,
                    error_classification="PERMANENT_NETWORK",
                )
                before = evidence.attempt_receipt_path.stat()
                repeated = store.quarantine_bodyless_attempt(
                    query,
                    number,
                    capture,
                    error_classification="PERMANENT_NETWORK",
                )
                after = evidence.attempt_receipt_path.stat()

            self.assertEqual(repeated, evidence)
            self.assertEqual((after.st_dev, after.st_ino, after.st_mtime_ns),
                             (before.st_dev, before.st_ino, before.st_mtime_ns))
            attempt_bytes = evidence.attempt_receipt_path.read_bytes()
            self.assertEqual(
                evidence.attempt_receipt_sha256,
                hashlib.sha256(attempt_bytes).hexdigest(),
            )
            attempt = json.loads(attempt_bytes)
            self.assertEqual(attempt["outcome"], "QUARANTINED")
            self.assertIsNone(attempt["body_sha256"])
            self.assertEqual(attempt["byte_count"], 0)
            receipt = json.loads(evidence.evidence_receipt_path.read_bytes())
            self.assertEqual(receipt["evidence_kind"], "TERMINAL_BODYLESS_FAILURE")
            self.assertIsNone(receipt["quarantined_body_path"])
            self.assertIsNone(receipt["raw_sha256"])
            jsonschema.validate(receipt, json.loads(MANIFEST_SCHEMA.read_bytes()))
            progress = store.read_verified_query_progress(query)
            self.assertEqual(progress.status, "QUARANTINED")
            self.assertEqual(progress.pending_attempt_numbers, ())
            self.assertEqual(progress.quarantines, ())

    def test_bodyless_terminal_failure_rejects_tampered_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            capture = dataclasses.replace(
                _capture(None, status=None, error="PERMANENT_NETWORK"),
                final_url=None,
                response_headers=(),
            )
            with store.acquire_query(query) as lease:
                evidence = store.quarantine_bodyless_attempt(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    error_classification="PERMANENT_NETWORK",
                )
            receipt = json.loads(evidence.evidence_receipt_path.read_bytes())
            receipt["evidence_kind"] = "PARTIAL_SPLIT_RESPONSE"
            evidence.evidence_receipt_path.write_bytes(_canonical_file(receipt))
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "evidence|bodyless|binding|path"
            ):
                store.read_verified_query_progress(query)

    def test_partial_split_persists_bytes_and_latest_split_receipt_binding(self) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            requested_range={
                "year": 2021,
                "start_date": "2021-06-01",
                "end_date": "2021-06-30",
            },
            expected_media_type="text/csv",
        )
        for reason in ("SIZE_LIMIT", "ROW_CAP", "TIMEOUT"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                partial = b"game_pk,pitch_type\n1,FF\n"
                capture = dataclasses.replace(
                    _capture(
                        partial,
                        error=(
                            "TRANSIENT_NETWORK" if reason == "TIMEOUT" else reason
                        ),
                    ),
                    response_headers=(("Content-Type", "text/csv"),),
                    timed_out=reason == "TIMEOUT",
                )
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    evidence = store.persist_split_attempt(
                        query, number, capture, reason=reason
                    )
                    body_before = evidence.body_path.stat()
                    repeated = store.persist_split_attempt(
                        query, number, capture, reason=reason
                    )
                    body_after = evidence.body_path.stat()

                self.assertEqual(repeated, evidence)
                self.assertEqual(
                    (body_after.st_dev, body_after.st_ino, body_after.st_mtime_ns),
                    (body_before.st_dev, body_before.st_ino, body_before.st_mtime_ns),
                )
                self.assertEqual(evidence.body_path.read_bytes(), partial)
                self.assertEqual(
                    evidence.body_sha256, hashlib.sha256(partial).hexdigest()
                )
                receipt = json.loads(evidence.evidence_receipt_path.read_bytes())
                self.assertEqual(receipt["evidence_kind"], "PARTIAL_SPLIT_RESPONSE")
                self.assertEqual(receipt["raw_sha256"], evidence.body_sha256)
                self.assertEqual(root / receipt["quarantined_body_path"], evidence.body_path)
                manifest_schema = json.loads(MANIFEST_SCHEMA.read_bytes())
                jsonschema.validate(receipt, manifest_schema)
                empty_declared_body = {**receipt, "byte_count": 0}
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(empty_declared_body, manifest_schema)
                progress = store.read_verified_query_progress(query)
                latest = progress.attempt_receipts[-1]
                self.assertEqual(latest["outcome"], "SPLIT_REQUIRED")
                self.assertEqual(latest["error_classification"], reason)
                self.assertEqual(latest["body_sha256"], evidence.body_sha256)
                self.assertEqual(progress.pending_attempt_numbers, ())

    def test_partial_split_rejects_reason_and_detects_tampered_bytes(self) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            capture = _capture(b"partial", error="SIZE_LIMIT")
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                with self.assertRaisesRegex(ValueError, "split reason"):
                    store.persist_split_attempt(
                        query, number, capture, reason="TRANSIENT_NETWORK"
                    )
                evidence = store.persist_split_attempt(
                    query, number, capture, reason="SIZE_LIMIT"
                )
            evidence.body_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "body|hash|binding|byte"
            ):
                store.read_verified_query_progress(query)

    def test_interrupted_partial_split_publication_resumes_same_reservation(self) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            capture = _capture(b"partial", error="SIZE_LIMIT")
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("injected interruption"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "injected interruption"),
                ):
                    store.persist_split_attempt(
                        query, number, capture, reason="SIZE_LIMIT"
                    )

            with store.acquire_query(query) as lease:
                evidence = store.persist_split_attempt(
                    query, number, capture, reason="SIZE_LIMIT"
                )
            progress = store.read_verified_query_progress(query)
            self.assertEqual(progress.pending_attempt_numbers, ())
            self.assertEqual(progress.attempt_receipts[-1]["attempt_number"], number)
            self.assertEqual(progress.attempt_receipts[-1]["outcome"], "SPLIT_REQUIRED")
            self.assertEqual(evidence.body_path.read_bytes(), b"partial")

    def test_failure_transactions_recover_every_durable_boundary_without_number(self) -> None:
        statcast = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        cases = (
            (statcast, _capture(b"partial", error="SIZE_LIMIT"), "split", "SIZE_LIMIT"),
            (
                _query(),
                dataclasses.replace(
                    _capture(None, status=None, error="PERMANENT_NETWORK"),
                    final_url=None,
                    response_headers=(),
                ),
                "bodyless",
                "PERMANENT_NETWORK",
            ),
        )
        for query, capture, kind, classification in cases:
            for boundary in ("INTENT", "TRANSACTION", "BODY", "EVIDENCE", "ATTEMPT"):
                with (
                    self.subTest(kind=kind, boundary=boundary),
                    tempfile.TemporaryDirectory() as td,
                ):
                    root = _tmp(td) / "raw"
                    store = V52EvidenceStore(root)

                    original_write = v52_evidence._write_once_at
                    original_unlink = v52_evidence._unlink_exact_regular_at

                    def interrupt_write(
                        parent_fd: int,
                        name: str,
                        contents: bytes,
                        label: str,
                        **kwargs: object,
                    ) -> None:
                        if (
                            boundary == "INTENT"
                            and label == "failure transaction marker"
                        ):
                            raise RuntimeError("interrupted at INTENT")
                        if (
                            boundary == "BODY"
                            and label == "failure evidence receipt"
                        ) or (
                            boundary == "EVIDENCE"
                            and label == "attempt receipt"
                        ):
                            raise RuntimeError(f"interrupted at {boundary}")
                        original_write(
                            parent_fd, name, contents, label, **kwargs
                        )

                    def interrupt_unlink(
                        parent_fd: int,
                        name: str,
                        expected: bytes,
                        label: str,
                    ) -> None:
                        if (
                            boundary == "ATTEMPT"
                            and label == "failure transaction cleanup marker"
                        ):
                            raise RuntimeError("interrupted at ATTEMPT")
                        original_unlink(parent_fd, name, expected, label)

                    if boundary == "TRANSACTION":
                        interruption = patch.object(
                            store,
                            "_finish_failure_transaction",
                            side_effect=RuntimeError(
                                "interrupted at TRANSACTION"
                            ),
                        )
                    elif boundary in {"INTENT", "BODY", "EVIDENCE"}:
                        interruption = patch(
                            "mlb_backtest.v52_evidence._write_once_at",
                            side_effect=interrupt_write,
                        )
                    else:
                        interruption = patch(
                            "mlb_backtest.v52_evidence._unlink_exact_regular_at",
                            side_effect=interrupt_unlink,
                        )

                    with store.acquire_query(query) as lease:
                        number = store.next_attempt_number(lease)
                        with (
                            interruption,
                            self.assertRaisesRegex(RuntimeError, "interrupted"),
                        ):
                            if kind == "split":
                                store.persist_split_attempt(
                                    query, number, capture, reason=classification
                                )
                            else:
                                store.quarantine_bodyless_attempt(
                                    query,
                                    number,
                                    capture,
                                    error_classification=classification,
                                )

                    restarted = V52EvidenceStore(root)
                    restarted.read_verified_query_progress(query)
                    with restarted.acquire_query(query):
                        recovered = restarted.recover_failure_attempt(query)
                    if boundary == "ATTEMPT":
                        self.assertIsNone(recovered)
                    else:
                        self.assertIsNotNone(recovered)
                        self.assertEqual(recovered.attempt_number, 1)
                    progress = restarted.read_verified_query_progress(query)
                    self.assertEqual(progress.pending_attempt_numbers, ())
                    self.assertFalse(
                        (
                            root
                            / "queries"
                            / query.source
                            / logical_query_id(query)
                            / "failure-transaction"
                        ).exists()
                    )
                    self.assertFalse(
                        (
                            root
                            / "queries"
                            / query.source
                            / logical_query_id(query)
                            / "failure-intent"
                        ).exists()
                    )

    def test_failure_marker_cannot_self_authenticate_against_external_intent(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            with store.acquire_query(query) as lease:
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    store.persist_split_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )

            query_root = root / "queries" / query.source / logical_query_id(query)
            self.assertTrue((query_root / "failure-intent/intent.json").is_file())
            marker_path = query_root / "failure-transaction/marker.json"
            marker = json.loads(marker_path.read_bytes())
            marker["evidence_receipt"]["error_classification"] = "ROW_CAP"
            base = {key: marker[key] for key in marker if key != "transaction_id"}
            marker["transaction_id"] = hashlib.sha256(
                v52_evidence._canonical_json_bytes(base)
            ).hexdigest()
            marker_path.write_bytes(_canonical_file(marker))

            restarted = V52EvidenceStore(root)
            with restarted.acquire_query(query), self.assertRaisesRegex(
                (ValueError, RuntimeError), "intent|transaction|marker|binding"
            ):
                restarted.recover_failure_attempt(query)

    def test_failure_recovery_rejects_intent_tamper_and_marker_without_intent(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for attack in ("intent", "intent-symlink", "marker-only"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    with (
                        patch.object(
                            store,
                            "_finish_failure_transaction",
                            side_effect=RuntimeError("interrupted"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "interrupted"),
                    ):
                        store.persist_split_attempt(
                            query,
                            store.next_attempt_number(lease),
                            _capture(b"partial", error="SIZE_LIMIT"),
                            reason="SIZE_LIMIT",
                        )
                query_root = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                intent_root = query_root / "failure-intent"
                if attack == "intent":
                    intent_path = intent_root / "intent.json"
                    intent = json.loads(intent_path.read_bytes())
                    intent["exact_reason"] = "ROW_CAP"
                    intent_path.write_bytes(_canonical_file(intent))
                elif attack == "intent-symlink":
                    intent_path = intent_root / "intent.json"
                    outside = root / "outside-intent.json"
                    outside.write_bytes(intent_path.read_bytes())
                    intent_path.unlink()
                    intent_path.symlink_to(outside)
                else:
                    (intent_root / "body.raw").unlink()
                    (intent_root / "intent.json").unlink()
                    intent_root.rmdir()

                restarted = V52EvidenceStore(root)
                with restarted.acquire_query(query), self.assertRaisesRegex(
                    (ValueError, RuntimeError), "intent|marker|binding|semantic"
                ):
                    restarted.recover_failure_attempt(query)

    def test_failure_intent_conflicting_retry_commits_only_original_reason(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    store.persist_split_attempt(
                        query, number, _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )

            restarted = V52EvidenceStore(root)
            with restarted.acquire_query(query), self.assertRaisesRegex(
                RuntimeError, "conflicting failure mutation"
            ):
                restarted.persist_split_attempt(
                    query, number, _capture(b"partial", error="ROW_CAP"),
                    reason="ROW_CAP",
                )
            progress = restarted.read_verified_query_progress(query)
            self.assertEqual(
                progress.attempt_receipts[-1]["error_classification"],
                "SIZE_LIMIT",
            )
            self.assertFalse(
                (
                    root / "queries" / query.source / logical_query_id(query)
                    / "failure-intent"
                ).exists()
            )

    def test_failure_recovery_rejects_last_boundary_root_and_query_swaps(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for generation in ("root", "query"):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    with (
                        patch.object(
                            store,
                            "_finish_failure_transaction",
                            side_effect=RuntimeError("interrupted"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "interrupted"),
                    ):
                        store.persist_split_attempt(
                            query,
                            store.next_attempt_number(lease),
                            _capture(b"partial", error="SIZE_LIMIT"),
                            reason="SIZE_LIMIT",
                        )
                query_dir = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                restarted = V52EvidenceStore(root)
                original = v52_evidence._read_regular_at
                swapped = False

                def swap_after_marker_read(*args: object, **kwargs: object):
                    nonlocal swapped
                    result = original(*args, **kwargs)  # type: ignore[arg-type]
                    label = args[2] if len(args) > 2 else kwargs.get("label")
                    if not swapped and label == "failure transaction marker":
                        swapped = True
                        target = root if generation == "root" else query_dir
                        displaced = target.with_name(target.name + "-displaced")
                        target.rename(displaced)
                        target.mkdir()
                    return result

                lease_context = restarted.acquire_query(query)
                lease_context.__enter__()
                try:
                    with (
                        patch.object(
                            v52_evidence,
                            "_read_regular_at",
                            side_effect=swap_after_marker_read,
                        ),
                        self.assertRaisesRegex(
                            (ValueError, RuntimeError),
                            "root|query|identity|generation|chain",
                        ),
                    ):
                        restarted.recover_failure_attempt(query)
                finally:
                    with contextlib.suppress(ValueError):
                        lease_context.__exit__(None, None, None)
                self.assertTrue(swapped)
                displaced_attempt = (
                    root.with_name(root.name + "-displaced")
                    / "queries"
                    / query.source
                    / logical_query_id(query)
                    / "attempts/000001.json"
                    if generation == "root"
                    else query_dir.with_name(query_dir.name + "-displaced")
                    / "attempts/000001.json"
                )
                self.assertFalse(displaced_attempt.exists())

    def test_failure_finish_rebinds_immutable_intent_identity(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            original = store._verified_quarantine_bundle
            swapped = False

            def swap_intent_after_deep_verify(*args: object, **kwargs: object):
                nonlocal swapped
                result = original(*args, **kwargs)  # type: ignore[arg-type]
                if not swapped:
                    swapped = True
                    intent = (
                        root / "queries" / query.source / logical_query_id(query)
                        / "failure-intent/intent.json"
                    )
                    contents = intent.read_bytes()
                    intent.unlink()
                    intent.write_bytes(contents)
                return result

            with store.acquire_query(query) as lease:
                with (
                    patch.object(
                        store,
                        "_verified_quarantine_bundle",
                        side_effect=swap_intent_after_deep_verify,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "intent|identity|changed|immutable",
                    ),
                ):
                    store.persist_split_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
            self.assertTrue(swapped)

    def test_generic_attempt_writer_rejects_split_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                with self.assertRaisesRegex(ValueError, "dedicated"):
                    store.record_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(None, status=None, error="TIMEOUT"),
                        outcome="SPLIT_REQUIRED",
                    )

    def test_generic_attempt_writer_rejects_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                with self.assertRaisesRegex(ValueError, "dedicated"):
                    store.record_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"invalid"),
                        outcome="QUARANTINED",
                    )

    def test_pending_failure_transaction_is_closed_before_every_mutation(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for mutation in (
            "next", "record", "publish", "quarantine", "bodyless", "split"
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                split_capture = _capture(b"partial", error="SIZE_LIMIT")
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    with (
                        patch.object(
                            store,
                            "_finish_failure_transaction",
                            side_effect=RuntimeError("interrupted after transaction rename"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "interrupted"),
                    ):
                        store.persist_split_attempt(
                            query, number, split_capture, reason="SIZE_LIMIT"
                        )

                restarted = V52EvidenceStore(root)
                with restarted.acquire_query(query) as lease:
                    if mutation == "next":
                        with self.assertRaisesRegex(RuntimeError, "recovered"):
                            restarted.next_attempt_number(lease)
                    elif mutation == "record":
                        with self.assertRaisesRegex(RuntimeError, "recovered"):
                            restarted.record_attempt(
                                query, number,
                                _capture(None, status=None, error="TRANSIENT_NETWORK"),
                                outcome="TRANSIENT_NETWORK",
                            )
                    elif mutation == "publish":
                        with self.assertRaisesRegex(RuntimeError, "recovered"):
                            restarted.publish_fetched(
                                query, number, _capture(b'{"dates":[]}'), _sidecar()
                            )
                    elif mutation == "quarantine":
                        with self.assertRaisesRegex(RuntimeError, "recovered"):
                            restarted.quarantine_attempt(
                                query, number, _capture(b"invalid"),
                                error_classification="SEMANTIC_INVALID",
                            )
                    elif mutation == "bodyless":
                        bodyless = dataclasses.replace(
                            _capture(None, status=None, error="PERMANENT_NETWORK"),
                            final_url=None,
                            response_headers=(),
                        )
                        with self.assertRaisesRegex(RuntimeError, "recovered"):
                            restarted.quarantine_bodyless_attempt(
                                query, number, bodyless,
                                error_classification="PERMANENT_NETWORK",
                            )
                    else:
                        recovered = restarted.persist_split_attempt(
                            query, number, split_capture, reason="SIZE_LIMIT"
                        )
                        self.assertEqual(recovered.outcome, "SPLIT_REQUIRED")

                progress = restarted.read_verified_query_progress(query)
                self.assertEqual(
                    tuple(item["outcome"] for item in progress.attempt_receipts),
                    ("SPLIT_REQUIRED",),
                )
                self.assertEqual(progress.pending_attempt_numbers, ())
                query_root = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                self.assertFalse((query_root / "failure-transaction").exists())
                self.assertFalse((query_root / "success.json").exists())
                self.assertFalse((query_root / "attempts/000002.reservation.json").exists())

    def test_split_partial_body_must_be_nonempty_and_empty_timeout_normalizes(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for reason in ("SIZE_LIMIT", "ROW_CAP"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td:
                store = V52EvidenceStore(_tmp(td) / "raw")
                with store.acquire_query(query) as lease:
                    with self.assertRaisesRegex(ValueError, "nonempty"):
                        store.persist_split_attempt(
                            query, store.next_attempt_number(lease),
                            _capture(b"", error=reason), reason=reason,
                        )

        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            timeout = dataclasses.replace(
                _capture(b"", status=None, error="TRANSIENT_NETWORK"),
                final_url=None,
                response_headers=(),
                timed_out=True,
            )
            with store.acquire_query(query) as lease:
                evidence = store.persist_split_attempt(
                    query, store.next_attempt_number(lease), timeout, reason="TIMEOUT"
                )
            receipt = json.loads(evidence.evidence_receipt_path.read_bytes())
            self.assertEqual(receipt["byte_count"], 0)
            self.assertIsNone(receipt["body_sha256"])
            self.assertIsNone(receipt["raw_sha256"])
            self.assertIsNone(evidence.body_path)
            jsonschema.validate(receipt, json.loads(MANIFEST_SCHEMA.read_bytes()))

    def test_pre_rename_failure_transaction_failures_leave_no_membership(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for failure in ("marker-write", "body-write", "rename", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                original_write = v52_evidence._write_once_at
                original_rename = os.rename
                original_fsync = os.fsync
                directory_fsyncs = 0

                def fail_write(
                    parent_fd: int, name: str, contents: bytes, label: str, **kwargs: object
                ) -> None:
                    if failure == "marker-write" and label == "failure intent marker":
                        raise OSError("injected marker write failure")
                    if failure == "body-write" and label == "failure intent body":
                        raise OSError("injected body write failure")
                    original_write(parent_fd, name, contents, label, **kwargs)

                def fail_rename(src: object, dst: object, *args: object, **kwargs: object) -> None:
                    if failure == "rename" and dst == "failure-intent":
                        raise OSError("injected rename failure")
                    original_rename(src, dst, *args, **kwargs)

                def fail_fsync(descriptor: int) -> None:
                    nonlocal directory_fsyncs
                    if failure == "fsync" and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                        directory_fsyncs += 1
                        if directory_fsyncs == 3:
                            raise OSError("injected fsync failure")
                    original_fsync(descriptor)

                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    with (
                        patch("mlb_backtest.v52_evidence._write_once_at", side_effect=fail_write),
                        patch("mlb_backtest.v52_evidence.os.rename", side_effect=fail_rename),
                        patch("mlb_backtest.v52_evidence.os.fsync", side_effect=fail_fsync),
                        self.assertRaisesRegex(OSError, "injected"),
                    ):
                        store.persist_split_attempt(
                            query, number, _capture(b"partial", error="SIZE_LIMIT"),
                            reason="SIZE_LIMIT",
                        )

                query_root = root / "queries" / query.source / logical_query_id(query)
                self.assertFalse((query_root / "failure-transaction").exists())
                self.assertEqual(
                    tuple(path.name for path in query_root.iterdir()
                          if path.name.startswith(".failure-transaction")),
                    (),
                )
                progress = store.read_verified_query_progress(query)
                self.assertEqual(progress.pending_attempt_numbers, (1,))

    def test_committed_failure_cleanup_crashes_resume_from_owned_tombstones(
        self,
    ) -> None:
        failure_points = (
            "after-transaction-claim",
            "after-transaction-container",
            "after-transaction-rename",
            "after-intent-rename",
            "after-transaction-unlink",
            "before-transaction-rmdir",
            "after-intent-unlink",
            "before-intent-rmdir",
            "after-intent-claim-unlink",
            "after-transaction-claim-unlink",
        )
        for failure_point in failure_points:
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                capture = dataclasses.replace(
                    _capture(None, status=None, error="PERMANENT_NETWORK"),
                    final_url=None,
                    response_headers=(),
                )
                original_rename = v52_evidence._rename_directory_exclusive_at
                original_claim = v52_evidence._publish_cleanup_claim_at
                original_mkdir = os.mkdir
                original_unlink = v52_evidence._unlink_exact_regular_at
                original_rmdir = os.rmdir

                def crash_after_claim(
                    parent_fd: int, name: str, contents: bytes
                ) -> None:
                    original_claim(parent_fd, name, contents)
                    if (
                        failure_point == "after-transaction-claim"
                        and "claim-transaction" in name
                    ):
                        raise OSError("injected cleanup claim crash")

                def crash_after_container(
                    name: object,
                    mode: int = 0o777,
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    original_mkdir(name, mode, *args, **kwargs)
                    if (
                        failure_point == "after-transaction-container"
                        and isinstance(name, str)
                        and name.startswith(".failure-cleanup-transaction-")
                    ):
                        raise OSError("injected cleanup container crash")

                def crash_after_rename(
                    src: object, dst: object, *args: object, **kwargs: object
                ) -> None:
                    original_rename(src, dst, *args, **kwargs)
                    if (
                        failure_point == "after-transaction-rename"
                        and src == "failure-transaction"
                        and dst == "payload"
                    ) or (
                        failure_point == "after-intent-rename"
                        and src == "failure-intent"
                        and dst == "payload"
                    ):
                        raise OSError("injected cleanup rename crash")

                def crash_after_unlink(
                    parent_fd: int,
                    name: str,
                    expected: bytes,
                    label: str,
                ) -> None:
                    original_unlink(parent_fd, name, expected, label)
                    if (
                        failure_point == "after-transaction-unlink"
                        and label == "failure transaction cleanup marker"
                    ) or (
                        failure_point == "after-intent-unlink"
                        and label == "failure intent cleanup marker"
                    ) or (
                        failure_point == "after-intent-claim-unlink"
                        and label == "failure intent cleanup claim"
                    ) or (
                        failure_point == "after-transaction-claim-unlink"
                        and label == "failure transaction cleanup claim"
                    ):
                        raise OSError("injected cleanup unlink crash")

                def crash_before_rmdir(
                    name: object, *args: object, **kwargs: object
                ) -> None:
                    if (
                        failure_point == "before-transaction-rmdir"
                        and isinstance(name, str)
                        and name.startswith(".failure-cleanup-transaction-")
                    ) or (
                        failure_point == "before-intent-rmdir"
                        and isinstance(name, str)
                        and name.startswith(".failure-cleanup-intent-")
                    ):
                        raise OSError("injected cleanup rmdir crash")
                    original_rmdir(name, *args, **kwargs)

                with store.acquire_query(query) as lease:
                    with (
                        patch(
                            "mlb_backtest.v52_evidence._publish_cleanup_claim_at",
                            side_effect=crash_after_claim,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence.os.mkdir",
                            side_effect=crash_after_container,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence._rename_directory_exclusive_at",
                            side_effect=crash_after_rename,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence._unlink_exact_regular_at",
                            side_effect=crash_after_unlink,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence.os.rmdir",
                            side_effect=crash_before_rmdir,
                        ),
                        self.assertRaisesRegex(OSError, "injected cleanup"),
                    ):
                        store.quarantine_bodyless_attempt(
                            query,
                            store.next_attempt_number(lease),
                            capture,
                            error_classification="PERMANENT_NETWORK",
                        )

                query_root = root / "queries" / query.source / logical_query_id(query)
                if failure_point == "after-intent-claim-unlink":
                    claim_names = {
                        path.name
                        for path in query_root.iterdir()
                        if "failure-cleanup-claim" in path.name
                    }
                    self.assertTrue(
                        any("claim-transaction" in name for name in claim_names)
                    )
                    self.assertFalse(
                        any("claim-intent" in name for name in claim_names)
                    )
                restarted = V52EvidenceStore(root)
                progress = restarted.read_verified_query_progress(query)
                self.assertEqual(progress.status, "QUARANTINED")
                self.assertEqual(progress.pending_attempt_numbers, ())
                self.assertEqual(
                    tuple(record["outcome"] for record in progress.attempt_receipts),
                    ("QUARANTINED",),
                )
                self.assertEqual(
                    tuple(
                        name
                        for name in os.listdir(query_root)
                        if name.startswith(".failure-cleanup-")
                    ),
                    (),
                )

    def test_failure_cleanup_tombstones_reject_forgery_symlink_and_conflict(
        self,
    ) -> None:
        for attack in ("forgery", "claim-forgery", "symlink", "conflict"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                capture = dataclasses.replace(
                    _capture(None, status=None, error="PERMANENT_NETWORK"),
                    final_url=None,
                    response_headers=(),
                )
                original_rename = v52_evidence._rename_directory_exclusive_at

                def interrupt_after_transaction_rename(
                    src: object, dst: object, *args: object, **kwargs: object
                ) -> None:
                    original_rename(src, dst, *args, **kwargs)
                    if src == "failure-transaction" and dst == "payload":
                        raise OSError("interrupted after transaction tombstone")

                with store.acquire_query(query) as lease:
                    with (
                        patch(
                            "mlb_backtest.v52_evidence._rename_directory_exclusive_at",
                            side_effect=interrupt_after_transaction_rename,
                        ),
                        self.assertRaisesRegex(OSError, "transaction tombstone"),
                    ):
                        store.quarantine_bodyless_attempt(
                            query,
                            store.next_attempt_number(lease),
                            capture,
                            error_classification="PERMANENT_NETWORK",
                        )

                query_root = root / "queries" / query.source / logical_query_id(query)
                transaction_tombstone = next(
                    path
                    for path in query_root.iterdir()
                    if path.name.startswith(".failure-cleanup-transaction-")
                    and path.is_dir()
                )
                victim = _tmp(td) / "victim"
                victim.mkdir()
                (victim / "not-owned").write_bytes(b"preserve")
                if attack == "forgery":
                    forged = query_root / (
                        ".failure-cleanup-intent-000001-" + "0" * 64
                    )
                    forged.mkdir()
                    (forged / "not-owned").write_bytes(b"foreign")
                elif attack == "claim-forgery":
                    forged_claim = query_root / (
                        ".failure-cleanup-claim-transaction-000001-"
                        + "0" * 32
                        + ".json"
                    )
                    forged_claim.write_bytes(b"{}\n")
                elif attack == "symlink":
                    shutil.rmtree(transaction_tombstone)
                    transaction_tombstone.symlink_to(
                        victim, target_is_directory=True
                    )
                else:
                    conflicting = query_root / (
                        ".failure-cleanup-transaction-000001-" + "0" * 64
                    )
                    conflicting.mkdir()

                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    V52EvidenceStore(root).read_verified_query_progress(query)
                self.assertEqual((victim / "not-owned").read_bytes(), b"preserve")
                if attack == "forgery":
                    self.assertEqual((forged / "not-owned").read_bytes(), b"foreign")
                elif attack == "claim-forgery":
                    self.assertEqual(forged_claim.read_bytes(), b"{}\n")
                elif attack == "symlink":
                    self.assertTrue(transaction_tombstone.is_symlink())
                else:
                    self.assertTrue(conflicting.is_dir())

    def test_failure_cleanup_container_claim_is_exclusive_and_payload_rename_never_replaces(
        self,
    ) -> None:
        for attack in ("old-exact-name", "nonce-collision", "payload-race"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                query_id = store.register_query(query)
                query_root = root / "queries" / query.source / query_id
                capture = dataclasses.replace(
                    _capture(None, status=None, error="PERMANENT_NETWORK"),
                    final_url=None,
                    response_headers=(),
                )
                original_name = v52_evidence._failure_cleanup_tombstone_name
                original_mkdir = os.mkdir
                original_rename = v52_evidence._rename_directory_exclusive_at
                planted: Path | None = None
                planted_identity: tuple[int, int] | None = None

                def plant_old_exact_name(
                    kind: str,
                    attempt_number: int,
                    marker_contents: bytes,
                    **kwargs: object,
                ) -> str:
                    nonlocal planted
                    name = original_name(
                        kind, attempt_number, marker_contents, **kwargs
                    )
                    if attack == "old-exact-name" and kind == "transaction":
                        planted = query_root / name
                        planted.mkdir()
                    return name

                def collide_container(
                    name: object,
                    mode: int = 0o777,
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    nonlocal planted
                    if (
                        attack == "nonce-collision"
                        and isinstance(name, str)
                        and name.startswith(".failure-cleanup-")
                    ):
                        original_mkdir(name, mode, *args, **kwargs)
                        planted = query_root / name
                        (planted / "foreign").write_bytes(b"preserve")
                    original_mkdir(name, mode, *args, **kwargs)

                def race_payload(
                    src: object, dst: object, *args: object, **kwargs: object
                ) -> None:
                    nonlocal planted, planted_identity
                    if attack == "payload-race" and dst == "payload":
                        container_fd = kwargs["dst_dir_fd"]
                        original_mkdir("payload", 0o700, dir_fd=container_fd)
                        planted = next(
                            path
                            for path in query_root.iterdir()
                            if path.name.startswith(".failure-cleanup-")
                            and path.is_dir()
                        ) / "payload"
                        metadata = planted.lstat()
                        planted_identity = (metadata.st_dev, metadata.st_ino)
                    original_rename(src, dst, *args, **kwargs)

                with store.acquire_query(query) as lease:
                    with (
                        patch(
                            "mlb_backtest.v52_evidence._failure_cleanup_tombstone_name",
                            side_effect=plant_old_exact_name,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence.os.mkdir",
                            side_effect=collide_container,
                        ),
                        patch(
                            "mlb_backtest.v52_evidence._rename_directory_exclusive_at",
                            side_effect=race_payload,
                        ),
                        self.assertRaises((FileExistsError, ValueError, RuntimeError)),
                    ):
                        store.quarantine_bodyless_attempt(
                            query,
                            store.next_attempt_number(lease),
                            capture,
                            error_classification="PERMANENT_NETWORK",
                        )

                self.assertIsNotNone(planted)
                assert planted is not None
                self.assertTrue(planted.exists())
                if attack == "nonce-collision":
                    self.assertEqual((planted / "foreign").read_bytes(), b"preserve")
                elif attack == "payload-race":
                    metadata = planted.lstat()
                    self.assertEqual(
                        (metadata.st_dev, metadata.st_ino), planted_identity
                    )

    def test_failure_cleanup_recovery_errors_close_provisional_descriptors(
        self,
    ) -> None:
        descriptor_directory = Path("/dev/fd")
        if not descriptor_directory.is_dir():
            self.skipTest("descriptor inventory is unavailable")
        query = _query()
        capture = dataclasses.replace(
            _capture(None, status=None, error="PERMANENT_NETWORK"),
            final_url=None,
            response_headers=(),
        )
        for failure in ("rename", "list", "rebind"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                original_rename = v52_evidence._rename_directory_exclusive_at

                def interrupt_after_transaction_claim(
                    source: str,
                    destination: str,
                    **kwargs: int,
                ) -> None:
                    original_rename(
                        source,
                        destination,
                        **kwargs,
                    )
                    if source == "failure-transaction" and destination == "payload":
                        raise OSError("leave claimed transaction tombstone")

                with store.acquire_query(query) as lease:
                    with (
                        patch.object(
                            v52_evidence,
                            "_rename_directory_exclusive_at",
                            side_effect=interrupt_after_transaction_claim,
                        ),
                        self.assertRaisesRegex(OSError, "claimed transaction"),
                    ):
                        store.quarantine_bodyless_attempt(
                            query,
                            store.next_attempt_number(lease),
                            capture,
                            error_classification="PERMANENT_NETWORK",
                        )

                query_root = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                tombstone = next(
                    path
                    for path in query_root.iterdir()
                    if path.name.startswith(".failure-cleanup-transaction-")
                    and path.is_dir()
                )
                (tombstone / "payload").rename(
                    query_root / "failure-transaction"
                )
                tombstone_metadata = tombstone.lstat()
                tombstone_identity = (
                    tombstone_metadata.st_dev,
                    tombstone_metadata.st_ino,
                )
                original_listdir = os.listdir
                original_rebind = v52_evidence._rebind_directory_at

                def fail_listdir(path: object):
                    if isinstance(path, int):
                        metadata = os.fstat(path)
                        if (metadata.st_dev, metadata.st_ino) == tombstone_identity:
                            raise OSError("injected tombstone list failure")
                    return original_listdir(path)  # type: ignore[arg-type]

                def fail_rename(
                    source: str,
                    destination: str,
                    **kwargs: int,
                ) -> None:
                    if source == "failure-transaction" and destination == "payload":
                        raise OSError("injected payload rename failure")
                    original_rename(source, destination, **kwargs)

                def fail_rebind(*args: object, **kwargs: object) -> None:
                    label = args[3] if len(args) > 3 else kwargs.get("label")
                    if label == "failure transaction cleanup payload":
                        raise ValueError("injected payload rebind failure")
                    original_rebind(*args, **kwargs)  # type: ignore[arg-type]

                injected = {
                    "rename": patch.object(
                        v52_evidence,
                        "_rename_directory_exclusive_at",
                        side_effect=fail_rename,
                    ),
                    "list": patch.object(
                        v52_evidence.os, "listdir", side_effect=fail_listdir
                    ),
                    "rebind": patch.object(
                        v52_evidence,
                        "_rebind_directory_at",
                        side_effect=fail_rebind,
                    ),
                }[failure]
                before = len(original_listdir(descriptor_directory))
                with injected, self.assertRaises((OSError, ValueError, RuntimeError)):
                    V52EvidenceStore(root).read_verified_query_progress(query)
                after = len(original_listdir(descriptor_directory))
                self.assertEqual(after, before)

    def test_failure_transaction_rejects_tampered_staged_body(self) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            with store.acquire_query(query) as lease:
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    store.persist_split_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
            transaction = (
                root / "queries" / query.source / logical_query_id(query)
                / "failure-intent" / "body.raw"
            )
            transaction.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "intent body|hash|byte"):
                store.read_verified_query_progress(query)
            with store.acquire_query(query), self.assertRaisesRegex(
                ValueError, "intent body|hash|byte"
            ):
                store.recover_failure_attempt(query)

    def test_failure_body_publication_never_replaces_a_racing_destination(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        body = b"authenticated-partial"
        body_name = f"{hashlib.sha256(body).hexdigest()}.raw"
        for race in ("identical", "foreign", "symlink"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                original_read = v52_evidence._read_regular_snapshot_at
                planted_identity: tuple[int, int] | None = None
                planted = False
                outside = root / "outside-body"
                outside.parent.mkdir(parents=True, exist_ok=True)
                outside.write_bytes(b"outside-preserved")

                def plant_after_missing_read(
                    parent_fd: int,
                    name: str,
                    label: str,
                    *,
                    missing_ok: bool = False,
                ):
                    nonlocal planted, planted_identity
                    snapshot = original_read(
                        parent_fd, name, label, missing_ok=missing_ok
                    )
                    if (
                        not planted
                        and snapshot is None
                        and missing_ok
                        and label == "persisted failure response body"
                    ):
                        planted = True
                        if race == "symlink":
                            os.symlink(outside, name, dir_fd=parent_fd)
                        else:
                            descriptor = os.open(
                                name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=parent_fd,
                            )
                            try:
                                os.write(
                                    descriptor,
                                    body if race == "identical" else b"foreign",
                                )
                            finally:
                                os.close(descriptor)
                        metadata = os.stat(
                            name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        planted_identity = (metadata.st_dev, metadata.st_ino)
                    return snapshot

                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    publication = contextlib.nullcontext()
                    if race != "identical":
                        publication = self.assertRaises(
                            (ValueError, RuntimeError, OSError)
                        )
                    with (
                        patch.object(
                            v52_evidence,
                            "_read_regular_snapshot_at",
                            side_effect=plant_after_missing_read,
                        ),
                        publication,
                    ):
                        store.persist_split_attempt(
                            query,
                            number,
                            _capture(body, error="SIZE_LIMIT"),
                            reason="SIZE_LIMIT",
                        )

                self.assertTrue(planted)
                final_body = (
                    root / "quarantine" / query.source
                    / logical_query_id(query) / body_name
                )
                metadata = final_body.lstat()
                self.assertEqual(
                    (metadata.st_dev, metadata.st_ino), planted_identity
                )
                if race == "identical":
                    self.assertEqual(final_body.read_bytes(), body)
                elif race == "foreign":
                    self.assertEqual(final_body.read_bytes(), b"foreign")
                else:
                    self.assertTrue(final_body.is_symlink())
                    self.assertEqual(outside.read_bytes(), b"outside-preserved")

    def test_failure_body_link_intermediate_recovers_only_exact_owned_pair(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        body = b"authenticated-partial"
        body_name = f"{hashlib.sha256(body).hexdigest()}.raw"
        for attack in (None, "different-inode", "extra-link", "symlink", "wrong-bytes"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query_root = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                final_body = (
                    root / "quarantine" / query.source
                    / logical_query_id(query) / body_name
                )
                original_unlink = os.unlink
                interrupted = False

                def interrupt_before_source_unlink(
                    name: object,
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    nonlocal interrupted
                    if (
                        not interrupted
                        and name == "body.raw"
                        and final_body.exists()
                    ):
                        interrupted = True
                        source_metadata = os.stat(
                            name,
                            dir_fd=kwargs.get("dir_fd"),
                            follow_symlinks=False,
                        )
                        destination_metadata = final_body.lstat()
                        self.assertEqual(source_metadata.st_nlink, 2)
                        self.assertEqual(
                            (source_metadata.st_dev, source_metadata.st_ino),
                            (destination_metadata.st_dev, destination_metadata.st_ino),
                        )
                        raise OSError("interrupted after destination link")
                    original_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

                with store.acquire_query(query) as lease:
                    with (
                        patch.object(
                            v52_evidence.os,
                            "unlink",
                            side_effect=interrupt_before_source_unlink,
                        ),
                        self.assertRaisesRegex(OSError, "destination link"),
                    ):
                        store.persist_split_attempt(
                            query,
                            store.next_attempt_number(lease),
                            _capture(body, error="SIZE_LIMIT"),
                            reason="SIZE_LIMIT",
                        )
                self.assertTrue(interrupted)
                staged_body = query_root / "failure-intent/body.raw"
                self.assertEqual(staged_body.stat().st_nlink, 2)
                self.assertEqual(final_body.stat().st_nlink, 2)

                extra = root / "not-owned-extra-link"
                outside = root / "outside-body"
                if attack == "different-inode":
                    final_body.unlink()
                    os.link(staged_body, extra)
                    final_body.write_bytes(body)
                elif attack == "extra-link":
                    os.link(staged_body, extra)
                elif attack == "symlink":
                    final_body.unlink()
                    os.link(staged_body, extra)
                    outside.write_bytes(body)
                    final_body.symlink_to(outside)
                elif attack == "wrong-bytes":
                    final_body.write_bytes(b"tampered")

                restarted = V52EvidenceStore(root)
                if attack is None:
                    progress = restarted.read_verified_query_progress(query)
                    self.assertEqual(progress.pending_attempt_numbers, (1,))
                    with restarted.acquire_query(query):
                        recovered = restarted.recover_failure_attempt(query)
                    self.assertIsNotNone(recovered)
                    assert recovered is not None
                    self.assertEqual(recovered.outcome, "SPLIT_REQUIRED")
                    self.assertEqual(final_body.read_bytes(), body)
                    self.assertEqual(final_body.stat().st_nlink, 1)
                    self.assertFalse(staged_body.exists())
                else:
                    with self.assertRaises((ValueError, RuntimeError, OSError)):
                        restarted.read_verified_query_progress(query)
                    with restarted.acquire_query(query), self.assertRaises(
                        (ValueError, RuntimeError, OSError)
                    ):
                        restarted.recover_failure_attempt(query)
                    if attack == "wrong-bytes":
                        self.assertEqual(final_body.read_bytes(), b"tampered")
                    elif attack == "symlink":
                        self.assertTrue(final_body.is_symlink())
                    else:
                        self.assertTrue(final_body.exists())

    def test_partial_transaction_requires_its_staged_body_until_atomic_move(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for mutation in ("missing", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    with (
                        patch.object(
                            store,
                            "_finish_failure_transaction",
                            side_effect=RuntimeError("interrupted"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "interrupted"),
                    ):
                        store.persist_split_attempt(
                            query, store.next_attempt_number(lease),
                            _capture(b"partial", error="SIZE_LIMIT"), reason="SIZE_LIMIT"
                        )
                body = root / "queries" / query.source / logical_query_id(query) / "failure-intent/body.raw"
                body.unlink()
                if mutation == "symlink":
                    body.symlink_to(root / "outside")
                with self.assertRaisesRegex((ValueError, RuntimeError), "intent body|missing|regular"):
                    store.read_verified_query_progress(query)
                with store.acquire_query(query), self.assertRaisesRegex(
                    (ValueError, RuntimeError), "intent body|missing|regular"
                ):
                    store.recover_failure_attempt(query)

    def test_preexisting_failure_transaction_temp_directory_is_never_adopted(
        self,
    ) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            original_mkdir = os.mkdir
            collision: Path | None = None

            def collide_with_temporary(
                name: str | bytes,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> None:
                nonlocal collision
                if (
                    isinstance(name, str)
                    and name.startswith(".failure-transaction.")
                    and dir_fd is not None
                ):
                    original_mkdir(name, mode, dir_fd=dir_fd)
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd
                    )
                    try:
                        rogue_descriptor = os.open(
                            "not-owned",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=descriptor,
                        )
                        try:
                            os.write(rogue_descriptor, b"foreign")
                        finally:
                            os.close(rogue_descriptor)
                    finally:
                        os.close(descriptor)
                    query_root = (
                        root
                        / "queries"
                        / query.source
                        / logical_query_id(query)
                    )
                    collision = query_root / name
                    raise FileExistsError("injected temporary collision")
                original_mkdir(name, mode, dir_fd=dir_fd)

            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                query_root = (
                    root / "queries" / query.source / logical_query_id(query)
                )
                with (
                    patch(
                        "mlb_backtest.v52_evidence.os.mkdir",
                        side_effect=collide_with_temporary,
                    ),
                    self.assertRaisesRegex(FileExistsError, "collision"),
                ):
                    store.persist_split_attempt(
                        query,
                        number,
                        _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )

            self.assertIsNotNone(collision)
            assert collision is not None
            self.assertEqual(
                (collision / "not-owned").read_bytes(), b"foreign"
            )
            self.assertFalse((query_root / "failure-transaction").exists())
            self.assertTrue(
                (query_root / "attempts/000001.reservation.json").is_file()
            )

    def test_pending_failure_transaction_blocks_legacy_success_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            capture = dataclasses.replace(
                _capture(None, status=None, error="PERMANENT_NETWORK"),
                final_url=None,
                response_headers=(),
            )
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    store.quarantine_bodyless_attempt(
                        query,
                        number,
                        capture,
                        error_classification="PERMANENT_NETWORK",
                    )

            restarted = V52EvidenceStore(store.root)
            with restarted.acquire_query(query), self.assertRaisesRegex(
                RuntimeError, "pending failure transaction recovered"
            ):
                restarted.publish_legacy_reference(
                    query, reference, _sidecar()
                )
            progress = restarted.read_verified_query_progress(query)
            self.assertEqual(progress.status, "QUARANTINED")
            self.assertEqual(progress.pending_attempt_numbers, ())
            self.assertFalse(
                (
                    store.root
                    / "queries"
                    / query.source
                    / logical_query_id(query)
                    / "success.json"
                ).exists()
            )

    def test_verified_split_read_never_reopens_paths_after_locked_snapshots(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                expected = store.persist_split_attempt(
                    query, number, _capture(b"partial", error="SIZE_LIMIT"),
                    reason="SIZE_LIMIT",
                )
            with patch.object(Path, "read_bytes", side_effect=AssertionError("path reopen")):
                self.assertEqual(store.read_verified_split_attempt(query, number), expected)

    def test_verified_split_read_final_snapshots_follow_all_chain_checks(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for attack in ("attempt", "evidence", "body", "query"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    expected = store.persist_split_attempt(
                        query, number, _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
                query_root = root / "queries" / query.source / logical_query_id(query)
                receipt = json.loads(expected.evidence_receipt_path.read_bytes())
                targets = {
                    "attempt": expected.attempt_receipt_path,
                    "evidence": expected.evidence_receipt_path,
                    "body": root / receipt["quarantined_body_path"],
                    "query": query_root / "query.json",
                }
                target = targets[attack]
                original_contents = target.read_bytes()
                original_chain = store._verify_query_chain
                attacked = False

                def chain_then_replace(*args: object, **kwargs: object) -> None:
                    nonlocal attacked
                    original_chain(*args, **kwargs)
                    if not attacked:
                        attacked = True
                        replacement = query_root / f".replace-{attack}"
                        replacement.write_bytes(original_contents)
                        os.replace(replacement, target)

                with (
                    patch.object(store, "_verify_query_chain", side_effect=chain_then_replace),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    store.read_verified_split_attempt(query, number)

    def test_verified_split_read_rejects_last_boundary_generation_swaps(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for generation in ("root", "query", "quarantine"):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    store.persist_split_attempt(
                        query, number, _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
                query_id = logical_query_id(query)
                query_dir = root / "queries" / query.source / query_id
                quarantine_dir = root / "quarantine" / query.source / query_id
                original = v52_evidence._rebind_regular_snapshot_at
                swapped = False

                def swap_after_last_rebind(*args: object, **kwargs: object):
                    nonlocal swapped
                    result = original(*args, **kwargs)  # type: ignore[arg-type]
                    label = args[3] if len(args) > 3 else kwargs.get("label")
                    if not swapped and label == "split evidence receipt":
                        swapped = True
                        target = {"root": root, "query": query_dir,
                                  "quarantine": quarantine_dir}[generation]
                        displaced = target.with_name(target.name + "-displaced")
                        target.rename(displaced)
                        target.mkdir()
                    return result

                with (
                    patch.object(
                        v52_evidence, "_rebind_regular_snapshot_at",
                        side_effect=swap_after_last_rebind,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "root|query|quarantine|chain|identity|generation",
                    ),
                ):
                    store.read_verified_split_attempt(query, number)
                self.assertTrue(swapped)

    def test_verified_split_read_rebinds_registration_at_last_boundary(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for attack in ("changed", "symlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    store.persist_split_attempt(
                        query, number, _capture(b"partial", error="SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
                query_file = (
                    root / "queries" / query.source / logical_query_id(query)
                    / "query.json"
                )
                original = v52_evidence._rebind_regular_snapshot_at
                attacked = False

                def attack_after_last_evidence_rebind(
                    *args: object, **kwargs: object
                ) -> None:
                    nonlocal attacked
                    original(*args, **kwargs)  # type: ignore[arg-type]
                    label = args[3] if len(args) > 3 else kwargs.get("label")
                    if not attacked and label == "split evidence receipt":
                        attacked = True
                        if attack == "changed":
                            registration = json.loads(query_file.read_bytes())
                            registration["parser_version"] = "tampered"
                            query_file.write_bytes(_canonical_file(registration))
                        else:
                            outside = root / "outside-query.json"
                            outside.write_bytes(query_file.read_bytes())
                            query_file.unlink()
                            query_file.symlink_to(outside)

                with (
                    patch.object(
                        v52_evidence,
                        "_rebind_regular_snapshot_at",
                        side_effect=attack_after_last_evidence_rebind,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "logical query|registration|regular|identity|changed",
                    ),
                ):
                    store.read_verified_split_attempt(query, number)
                self.assertTrue(attacked)

    def test_bodyless_timeout_split_requires_transport_timeout_capture(self) -> None:
        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        invalid = (
            dataclasses.replace(_capture(None, status=None, error="TRANSIENT_NETWORK"), final_url=None, response_headers=()),
            dataclasses.replace(_capture(None, status=None, error="PERMANENT_NETWORK"), final_url=None, response_headers=()),
            _capture(b"partial", error="TRANSIENT_NETWORK"),
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                for capture in invalid:
                    with self.subTest(capture=capture), self.assertRaisesRegex(
                        ValueError, "timed_out|timeout"
                    ):
                        store.persist_split_attempt(query, number, capture, reason="TIMEOUT")

    def test_fetch_partial_timeout_persists_once_as_timeout_split(self) -> None:
        from mlb_backtest.v52_http import fetch_attempt

        query = dataclasses.replace(
            _query(), source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )

        class PartialTimeoutResponse:
            status = 200
            headers = {"Content-Type": "text/csv", "ETag": '"partial"'}

            def __init__(self) -> None:
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def geturl(self) -> str:
                return query.endpoint

            def read(self, _size: int = -1) -> bytes:
                self.read_count += 1
                if self.read_count == 1:
                    return b"authenticated-partial"
                raise socket.timeout("late response body")

        clock_values = iter(
            (
                datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 14, 12, 1, tzinfo=timezone.utc),
            )
        )
        capture = fetch_attempt(
            query,
            timeout=90,
            max_bytes=1024,
            opener=lambda *_args, **_kwargs: PartialTimeoutResponse(),
            clock=lambda: next(clock_values),
        )
        self.assertTrue(capture.timed_out)
        self.assertEqual(capture.error_classification, "TRANSIENT_NETWORK")

        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            with store.acquire_query(query) as lease:
                persisted = store.persist_split_attempt(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    reason="TIMEOUT",
                )
            progress = store.read_verified_query_progress(query)

            self.assertEqual(persisted.outcome, "SPLIT_REQUIRED")
            self.assertEqual(persisted.error_classification, "TIMEOUT")
            self.assertEqual(persisted.body_path.read_bytes(), capture.body)
            self.assertEqual(
                list(persisted.body_path.parent.glob("*.raw")),
                [persisted.body_path],
            )
            self.assertEqual(progress.attempt_receipts[-1]["outcome"], "SPLIT_REQUIRED")
            self.assertEqual(progress.attempt_receipts[-1]["error_classification"], "TIMEOUT")

    def test_split_writer_body_requirements_and_authoritative_read(self) -> None:
        query = dataclasses.replace(
            _query(),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            bodyless = dataclasses.replace(
                _capture(None, status=None, error="TRANSIENT_NETWORK"),
                final_url=None,
                response_headers=(),
                timed_out=True,
            )
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                for reason in ("SIZE_LIMIT", "ROW_CAP"):
                    with self.subTest(reason=reason), self.assertRaisesRegex(
                        ValueError, "body|bytes"
                    ):
                        store.persist_split_attempt(
                            query,
                            number,
                            bodyless,
                            reason=reason,
                        )
                written = store.persist_split_attempt(
                    query, number, bodyless, reason="TIMEOUT"
                )
            verified = store.read_verified_split_attempt(query, number)
            self.assertEqual(verified, written)
            self.assertIsNone(verified.body_path)
            self.assertIsNone(verified.body_sha256)
            self.assertEqual(verified.byte_count, 0)

    def test_quarantine_requires_a_reservation_before_writing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query):
                with self.assertRaisesRegex(RuntimeError, "reservation|missing"):
                    store.quarantine_attempt(
                        query,
                        1,
                        _capture(b"must-not-be-written"),
                        error_classification="SEMANTIC_INVALID",
                    )
            quarantine = root / "quarantine"
            self.assertFalse(quarantine.exists())

    def test_fetched_success_binds_query_attempt_raw_and_independent_sidecar(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            capture = _capture(b'{"dates":[{"date":"2022-04-07"}]}')
            with store.acquire_query(query) as lease:
                attempt = store.next_attempt_number(lease)
                published = store.publish_fetched(query, attempt, capture, _sidecar())

            self.assertEqual(published.storage_mode, "FETCHED_RAW")
            self.assertEqual(published, store.load_success(query))
            self.assertEqual(published, store.verify_success(query))
            success = json.loads(published.success_receipt_path.read_bytes())
            self.assertEqual(success["query_id"], logical_query_id(query))
            self.assertEqual(success["attempt_number"], attempt)
            self.assertEqual(
                success["raw_sha256"], hashlib.sha256(capture.body or b"").hexdigest()
            )
            self.assertIn("fetched_raw_path", success)
            self.assertNotIn("protected_legacy_reference_path", success)
            self.assertEqual(
                success["query_sha256"],
                hashlib.sha256((root / success["query_path"]).read_bytes()).hexdigest(),
            )

            sidecar_path = root / success["query_sidecar_path"]
            sidecar = json.loads(sidecar_path.read_bytes())
            self.assertEqual(
                set(sidecar),
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
            self.assertEqual(sidecar["semantics"], _sidecar())
            self.assertEqual(sidecar["byte_count"], len(capture.body or b""))
            self.assertEqual(sidecar["raw_sha256"], published.raw_sha256)

    def test_fetched_success_rejects_failure_or_raw_mismatched_tail_receipt(
        self,
    ) -> None:
        for attack in ("failure", "raw_mismatch"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    published = store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        _capture(),
                        _sidecar(),
                    )
                manifest = json.loads(published.success_receipt_path.read_bytes())
                attempt_path = root / manifest["attempt_history"][-1]["path"]
                attempt = json.loads(attempt_path.read_bytes())
                if attack == "failure":
                    attempt.update(
                        {
                            "error_classification": "TRANSIENT_HTTP",
                            "http_status": 503,
                            "outcome": "TRANSIENT_HTTP",
                        }
                    )
                else:
                    attempt["body_sha256"] = hashlib.sha256(b"other").hexdigest()
                attempt_contents = _canonical_file(attempt)
                attempt_path.write_bytes(attempt_contents)
                manifest["attempt_history"][-1]["sha256"] = hashlib.sha256(
                    attempt_contents
                ).hexdigest()
                published.success_receipt_path.write_bytes(_canonical_file(manifest))
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "attempt|success|raw|body|tail"
                ):
                    store.verify_success(query)
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "attempt|success|raw|body|tail"
                ):
                    store.load_success(query)

    def test_publish_rejects_old_finalized_attempt_before_success_and_recovers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            folder = root / "queries" / query.source / logical_query_id(query)
            with store.acquire_query(query) as lease:
                old_number = store.next_attempt_number(lease)
                store.record_attempt(query, old_number, _capture(), outcome="SUCCESS")
                newer_number = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    newer_number,
                    _capture(None, status=None, error="TIMEOUT"),
                    outcome="TRANSIENT_NETWORK",
                )
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "attempt|tail|current|success"
                ):
                    store.publish_fetched(query, old_number, _capture(), _sidecar())
                self.assertFalse((folder / "success.json").exists())
                recovery_number = store.next_attempt_number(lease)
                published = store.publish_fetched(
                    query, recovery_number, _capture(), _sidecar()
                )
            self.assertEqual(store.verify_success(query), published)

    def test_legacy_success_uses_exactly_one_protected_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            root = store.root
            with store.acquire_query(query):
                published = store.publish_legacy_reference(query, reference, _sidecar())
            self.assertEqual(published.storage_mode, "PROTECTED_LEGACY_REFERENCE")
            self.assertEqual(published.raw_sha256, reference["raw_sha256"])
            success = json.loads(published.success_receipt_path.read_bytes())
            self.assertNotIn("fetched_raw_path", success)
            self.assertIn("protected_legacy_reference_path", success)
            reference_path = root / success["protected_legacy_reference_path"]
            self.assertEqual(json.loads(reference_path.read_bytes()), reference)
            self.assertEqual(store.verify_success(query), published)

    def test_legacy_reference_requires_exact_task0_lineage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            missing = dict(reference)
            missing.pop("legacy_manifest_sha256")
            missing_query_id = dict(reference)
            missing_query_id.pop("current_query_id")
            extra = {**reference, "unreviewed_alias": "forbidden"}
            bad_semantics = {**reference, "semantic_sha256": "0" * 64}
            wrong_query_id = {**reference, "current_query_id": "0" * 64}
            for label, candidate in (
                ("missing", missing),
                ("missing-query-id", missing_query_id),
                ("extra", extra),
                ("semantic", bad_semantics),
                ("wrong-query-id", wrong_query_id),
            ):
                with self.subTest(label=label), store.acquire_query(query):
                    with self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "lineage|keys|semantic|reference",
                    ):
                        store.publish_legacy_reference(query, candidate, _sidecar())

    def test_legacy_success_reverifies_inventory_manifest_seal_and_payload(
        self,
    ) -> None:
        mutations = ("inventory", "manifest", "seal", "payload")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                store, query, reference = _legacy_fixture(_tmp(td))
                with store.acquire_query(query):
                    published = store.publish_legacy_reference(
                        query, reference, _sidecar()
                    )
                repository = store.root.parents[3]
                if mutation == "inventory":
                    target = repository / reference["protected_inventory_path"]
                elif mutation == "manifest":
                    target = repository / reference["legacy_manifest_path"]
                elif mutation == "seal":
                    target = repository / reference["legacy_seal_path"]
                else:
                    target = repository / reference["payload_path"]
                content = target.read_bytes()
                target.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
                with self.assertRaises((ValueError, RuntimeError)):
                    store.verify_success(query)
                self.assertTrue(published.success_receipt_path.exists())

    def test_verified_legacy_read_rejects_lineage_mutation_after_validation(
        self,
    ) -> None:
        for mutation in ("payload", "manifest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                store, query, reference = _legacy_fixture(_tmp(td))
                with store.acquire_query(query):
                    store.publish_legacy_reference(query, reference, _sidecar())
                repository = store.root.parents[3]
                target = repository / reference[
                    "payload_path"
                    if mutation == "payload"
                    else "legacy_manifest_path"
                ]
                original = store._validated_legacy_reference
                mutated = False

                def mutate_after_validation(*args: object, **kwargs: object):
                    nonlocal mutated
                    result = original(*args, **kwargs)  # type: ignore[arg-type]
                    if not mutated:
                        mutated = True
                        contents = target.read_bytes()
                        target.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
                    return result

                with (
                    patch.object(
                        store,
                        "_validated_legacy_reference",
                        side_effect=mutate_after_validation,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "legacy|lineage|payload|manifest|identity|changed",
                    ),
                ):
                    store.read_verified_success(query)

    def test_legacy_publish_rejects_hook_payload_mutation_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            repository = store.root.parents[3]
            payload_path = repository / reference["payload_path"]
            original_payload = payload_path.read_bytes()
            success_path = (
                store.root
                / "queries"
                / query.source
                / logical_query_id(query)
                / "success.json"
            )

            def corrupt_payload() -> None:
                payload_path.write_bytes(original_payload + b"corrupt")

            original_rebind = store._rebind_legacy_lineage

            def corrupt_payload_then_rebind(
                *args: object, **kwargs: object
            ) -> None:
                corrupt_payload()
                original_rebind(*args, **kwargs)  # type: ignore[arg-type]

            with store.acquire_query(query):
                with (
                    patch.object(
                        store,
                        "_rebind_legacy_lineage",
                        side_effect=corrupt_payload_then_rebind,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "legacy|lineage|payload|identity|changed",
                    ),
                ):
                    store.publish_legacy_reference(query, reference, _sidecar())
            self.assertFalse(success_path.exists())

            payload_path.write_bytes(original_payload)
            with store.acquire_query(query):
                published = store.publish_legacy_reference(
                    query, reference, _sidecar()
                )
            self.assertEqual(store.verify_success(query), published)

    def test_legacy_reference_rejects_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            repository = store.root.parents[3]
            audit = repository / "audit"
            displaced = repository / "audit-real"
            audit.rename(displaced)
            audit.symlink_to(displaced, target_is_directory=True)
            with store.acquire_query(query):
                with self.assertRaisesRegex(ValueError, "symlink|no-follow|ancestor"):
                    store.publish_legacy_reference(query, reference, _sidecar())

    def test_success_schemas_are_closed_and_enforce_exactly_one_storage_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query, store.next_attempt_number(lease), _capture(), _sidecar()
                )
            manifest = json.loads(published.success_receipt_path.read_bytes())
            sidecar = json.loads((root / manifest["query_sidecar_path"]).read_bytes())
            manifest_schema = json.loads(MANIFEST_SCHEMA.read_bytes())
            sidecar_schema = json.loads(SIDECAR_SCHEMA.read_bytes())
            jsonschema.Draft202012Validator.check_schema(manifest_schema)
            jsonschema.Draft202012Validator.check_schema(sidecar_schema)
            jsonschema.validate(manifest, manifest_schema)
            jsonschema.validate(sidecar, sidecar_schema)

            unknown = {**manifest, "unknown": True}
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(unknown, manifest_schema)
            mixed = {
                **manifest,
                "protected_legacy_reference_path": "objects/reference.json",
                "protected_legacy_reference_sha256": "0" * 64,
            }
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(mixed, manifest_schema)
            sidecar_unknown = {**sidecar, "aggregate_snapshot": {}}
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(sidecar_unknown, sidecar_schema)

        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            with store.acquire_query(query):
                published = store.publish_legacy_reference(
                    query, reference, _sidecar()
                )
            jsonschema.validate(
                json.loads(published.success_receipt_path.read_bytes()),
                manifest_schema,
            )

        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _derived_query()
            with store.acquire_query(query):
                published = store.publish_derived_disposition(
                    query, _derived_semantics()
                )
            jsonschema.validate(
                json.loads(published.success_receipt_path.read_bytes()),
                manifest_schema,
            )

    def test_query_lease_serializes_load_fetch_and_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            entered_first = threading.Event()
            allow_first = threading.Event()
            fetch_count = 0
            results: list[PublishedEvidence] = []
            errors: list[BaseException] = []

            def worker(index: int) -> None:
                nonlocal fetch_count
                try:
                    store = V52EvidenceStore(root)
                    with store.acquire_query(query) as lease:
                        existing = store.load_success(query)
                        if existing is not None:
                            results.append(existing)
                            return
                        fetch_count += 1
                        if index == 0:
                            entered_first.set()
                            self.assertTrue(allow_first.wait(timeout=5))
                        number = store.next_attempt_number(lease)
                        results.append(
                            store.publish_fetched(query, number, _capture(), _sidecar())
                        )
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=worker, args=(0,))
            second = threading.Thread(target=worker, args=(1,))
            first.start()
            self.assertTrue(entered_first.wait(timeout=5))
            second.start()
            time.sleep(0.1)
            self.assertEqual(fetch_count, 1)
            allow_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(fetch_count, 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])

    def test_partial_publication_interruption_is_resumable_and_success_is_last(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    with patch.object(
                        store,
                        "_verify_query_chain",
                        side_effect=RuntimeError("injected interruption"),
                    ):
                        store.publish_fetched(query, number, _capture(), _sidecar())

            folder = root / "queries" / query.source / logical_query_id(query)
            self.assertFalse((folder / "success.json").exists())
            self.assertGreaterEqual(len(list((folder / "objects").iterdir())), 1)
            self.assertGreaterEqual(len(list((folder / "sidecars").iterdir())), 1)
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                published = store.publish_fetched(query, number, _capture(), _sidecar())
            self.assertEqual(store.verify_success(query), published)
            self.assertEqual(len(list((folder / "objects").iterdir())), 1)
            self.assertEqual(len(list((folder / "sidecars").iterdir())), 1)

    def test_conflicting_retry_rejects_orphan_raw_without_writing_second_object(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            first_capture = _capture(b"orphan-a")
            with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                with store.acquire_query(query) as lease:
                    with patch.object(
                        store,
                        "_verify_query_chain",
                        side_effect=RuntimeError("injected interruption"),
                    ):
                        store.publish_fetched(
                            query,
                            store.next_attempt_number(lease),
                            first_capture,
                            _sidecar(),
                        )
            folder = root / "queries" / query.source / logical_query_id(query)
            original_names = {path.name for path in (folder / "objects").iterdir()}
            second_capture = _capture(b"conflicting-b")
            with store.acquire_query(query) as lease:
                with self.assertRaisesRegex(
                    RuntimeError, "duplicate|ambiguous|conflict"
                ):
                    store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        second_capture,
                        _sidecar(),
                    )
            self.assertEqual(
                {path.name for path in (folder / "objects").iterdir()},
                original_names,
            )
            self.assertNotIn(
                f"{hashlib.sha256(second_capture.body or b'').hexdigest()}.raw",
                original_names,
            )
            self.assertFalse((folder / "success.json").exists())

    def test_multiple_existing_content_members_are_duplicate_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            store.register_query(query)
            folder = root / "queries" / query.source / logical_query_id(query)
            for body in (b"orphan-a", b"orphan-b"):
                digest = hashlib.sha256(body).hexdigest()
                (folder / "objects" / f"{digest}.raw").write_bytes(body)
            with store.acquire_query(query) as lease:
                with self.assertRaisesRegex(RuntimeError, "duplicate|ambiguous"):
                    store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        _capture(b"orphan-a"),
                        _sidecar(),
                    )
            self.assertFalse((folder / "success.json").exists())

    def test_published_success_rejects_post_publish_content_ambiguity(self) -> None:
        for member in ("objects", "sidecars"):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        _capture(),
                        _sidecar(),
                    )
                folder = root / "queries" / query.source / logical_query_id(query)
                (folder / member / "unexpected").write_bytes(b"ambiguous")
                with self.assertRaisesRegex(RuntimeError, "duplicate|ambiguous"):
                    store.verify_success(query)
                with self.assertRaisesRegex(RuntimeError, "duplicate|ambiguous"):
                    store.load_success(query)

    def test_fetched_success_rejects_non_content_addressed_object_names(self) -> None:
        for member, path_key, suffix in (
            ("objects", "fetched_raw_path", ".raw"),
            ("sidecars", "query_sidecar_path", ".json"),
        ):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    published = store.publish_fetched(
                        query,
                        store.next_attempt_number(lease),
                        _capture(),
                        _sidecar(),
                    )
                manifest = json.loads(published.success_receipt_path.read_bytes())
                original = root / manifest[path_key]
                renamed = original.with_name(f"{'0' * 64}{suffix}")
                original.rename(renamed)
                manifest[path_key] = renamed.relative_to(root).as_posix()
                published.success_receipt_path.write_bytes(_canonical_file(manifest))
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "content|address|path|name|binding"
                ):
                    store.verify_success(query)

    def test_legacy_success_rejects_non_content_addressed_reference_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            with store.acquire_query(query):
                published = store.publish_legacy_reference(query, reference, _sidecar())
            manifest = json.loads(published.success_receipt_path.read_bytes())
            original = store.root / manifest["protected_legacy_reference_path"]
            renamed = original.with_name(f"{'0' * 64}.legacy-reference.json")
            original.rename(renamed)
            manifest["protected_legacy_reference_path"] = renamed.relative_to(
                store.root
            ).as_posix()
            published.success_receipt_path.write_bytes(_canonical_file(manifest))
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "content|address|path|name|binding"
            ):
                store.verify_success(query)

    def test_content_slot_open_failure_closes_already_opened_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(),
                    _sidecar(),
                )
            manifest = json.loads(published.success_receipt_path.read_bytes())
            object_path = root / manifest["fetched_raw_path"]
            sidecar_path = root / manifest["query_sidecar_path"]
            opened: list[int] = []
            object_open_count = 0
            real_open = v52_evidence._open_directory_at

            def fail_sidecar(parent_fd: int, name: str, *, create: bool) -> int:
                nonlocal object_open_count
                if name == "objects":
                    descriptor = real_open(parent_fd, name, create=create)
                    object_open_count += 1
                    if object_open_count == 2:
                        opened.append(descriptor)
                    return descriptor
                if name == "sidecars" and object_open_count == 2:
                    raise RuntimeError("injected sidecar open failure")
                return real_open(parent_fd, name, create=create)

            with (
                patch(
                    "mlb_backtest.v52_evidence._open_directory_at",
                    side_effect=fail_sidecar,
                ),
                self.assertRaisesRegex(RuntimeError, "injected"),
            ):
                store._verify_published_content_slots(
                    query,
                    object_name=object_path.name,
                    object_contents=object_path.read_bytes(),
                    sidecar_name=sidecar_path.name,
                    sidecar_contents=sidecar_path.read_bytes(),
                )
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])

    def test_query_directory_replacement_before_success_publish_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            query_id = logical_query_id(query)
            folder = root / "queries" / query.source / query_id
            moved = folder.with_name(f"{query_id}.moved")
            original_verify = store._verify_query_chain

            def replace_query_directory_then_verify(
                *args: object, **kwargs: object
            ) -> None:
                folder.rename(moved)
                folder.mkdir()
                original_verify(*args, **kwargs)  # type: ignore[arg-type]

            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "query|identity|binding"
            ):
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    with patch.object(
                        store,
                        "_verify_query_chain",
                        side_effect=replace_query_directory_then_verify,
                    ):
                        store.publish_fetched(query, number, _capture(), _sidecar())
            self.assertFalse((moved / "success.json").exists())
            self.assertFalse((folder / "success.json").exists())

    def test_lock_replacement_invalidates_active_lease_and_blocks_second_store(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            first_store = V52EvidenceStore(root)
            context = first_store.acquire_query(query)
            lease = context.__enter__()
            folder = root / "queries" / query.source / logical_query_id(query)
            lock_path = folder / ".query.lock"
            displaced = folder / ".query.lock.displaced"
            lock_path.rename(displaced)
            lock_path.write_bytes(b"")
            try:
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "lease|lock|identity"
                ):
                    first_store.next_attempt_number(lease)
            finally:
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "lease|lock|identity"
                ):
                    context.__exit__(None, None, None)

            second_store = V52EvidenceStore(root)
            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "ambiguous|lock|membership|registration|differ",
            ):
                with second_store.acquire_query(query):
                    self.fail("replacement lock must not create a second valid lease")

    def test_unlinked_and_recreated_lock_cannot_admit_a_second_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            first_store = V52EvidenceStore(root)
            context = first_store.acquire_query(query)
            lease = context.__enter__()
            folder = root / "queries" / query.source / logical_query_id(query)
            lock_path = folder / ".query.lock"
            lock_path.unlink()
            lock_path.write_bytes(b"")
            try:
                second_store = V52EvidenceStore(root)
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "registration|lock|identity|differ"
                ):
                    with second_store.acquire_query(query):
                        self.fail("replacement lock admitted a second store")
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "lease|lock|identity"
                ):
                    first_store.next_attempt_number(lease)
            finally:
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "lease|lock|identity"
                ):
                    context.__exit__(None, None, None)

    def test_second_store_can_acquire_unchanged_lock_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            query = _query()
            first_store = V52EvidenceStore(root)
            with first_store.acquire_query(query):
                pass
            second_store = V52EvidenceStore(root)
            with second_store.acquire_query(query) as lease:
                self.assertEqual(second_store.next_attempt_number(lease), 1)

    def test_publish_rechecks_lock_after_final_success_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _tmp(td) / "raw"
            store = V52EvidenceStore(root)
            query = _query()
            context = store.acquire_query(query)
            lease = context.__enter__()
            folder = root / "queries" / query.source / logical_query_id(query)
            lock_path = folder / ".query.lock"
            displaced = folder / ".query.lock.displaced"
            original_verify = store.verify_success

            def verify_then_replace_lock(candidate: LogicalQuery) -> PublishedEvidence:
                published = original_verify(candidate)
                lock_path.rename(displaced)
                lock_path.write_bytes(b"")
                return published

            try:
                number = store.next_attempt_number(lease)
                with (
                    patch.object(
                        store,
                        "verify_success",
                        side_effect=verify_then_replace_lock,
                    ),
                    self.assertRaisesRegex(
                        (ValueError, RuntimeError), "lease|lock|identity"
                    ),
                ):
                    store.publish_fetched(query, number, _capture(), _sidecar())
            finally:
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "lease|lock|identity"
                ):
                    context.__exit__(None, None, None)

    def test_published_success_is_terminal_for_attempt_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(_tmp(td) / "raw")
            query = _query()
            with store.acquire_query(query) as lease:
                published = store.publish_fetched(
                    query, store.next_attempt_number(lease), _capture(), _sidecar()
                )
            with store.acquire_query(query) as lease:
                self.assertEqual(store.load_success(query), published)
                with self.assertRaisesRegex(
                    RuntimeError, "already.*published|terminal"
                ):
                    store.next_attempt_number(lease)
            self.assertEqual(store.verify_success(query), published)

    def test_protected_success_blocks_finalizing_an_old_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store, query, reference = _legacy_fixture(_tmp(td))
            with store.acquire_query(query) as lease:
                old_number = store.next_attempt_number(lease)
            with store.acquire_query(query):
                published = store.publish_legacy_reference(query, reference, _sidecar())
            with store.acquire_query(query):
                with self.assertRaisesRegex(
                    RuntimeError, "already.*published|terminal"
                ):
                    store.record_attempt(
                        query,
                        old_number,
                        _capture(None, status=None, error="LATE"),
                        outcome="TRANSIENT_NETWORK",
                    )
            self.assertEqual(store.verify_success(query), published)

    def test_traversal_symlink_hardlink_and_special_file_attacks_fail_closed(
        self,
    ) -> None:
        for source in ("../escape", "nested/source", "nested\\source", ".", ".."):
            with self.subTest(source=source), self.assertRaises(ValueError):
                logical_query_id(_query(source=source))

        with tempfile.TemporaryDirectory() as td:
            base = _tmp(td)
            actual = base / "actual"
            actual.mkdir()
            linked_root = base / "linked"
            linked_root.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|no-follow|root"):
                V52EvidenceStore(linked_root)

            ancestor = base / "ancestor-link"
            ancestor.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|no-follow|root"):
                V52EvidenceStore(ancestor / "child")

        attack_cases = ("symlink", "hardlink", "fifo")
        for attack in attack_cases:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                store.register_query(query)
                query_path = (
                    root
                    / "queries"
                    / query.source
                    / logical_query_id(query)
                    / "query.json"
                )
                original = query_path.read_bytes()
                query_path.unlink()
                target = _tmp(td) / "target"
                target.write_bytes(original)
                if attack == "symlink":
                    query_path.symlink_to(target)
                elif attack == "hardlink":
                    os.link(target, query_path)
                else:
                    os.mkfifo(query_path)
                with self.assertRaises((ValueError, RuntimeError)):
                    store.register_query(query)

    def test_verify_rejects_tampered_symlinked_and_hardlinked_success_members(
        self,
    ) -> None:
        for attack in ("tamper", "symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _tmp(td) / "raw"
                store = V52EvidenceStore(root)
                query = _query()
                with store.acquire_query(query) as lease:
                    published = store.publish_fetched(
                        query, store.next_attempt_number(lease), _capture(), _sidecar()
                    )
                success = json.loads(published.success_receipt_path.read_bytes())
                raw_path = root / success["fetched_raw_path"]
                if attack == "tamper":
                    raw_path.write_bytes(b"changed")
                else:
                    original = raw_path.read_bytes()
                    raw_path.unlink()
                    target = _tmp(td) / "alias-target"
                    target.write_bytes(original)
                    if attack == "symlink":
                        raw_path.symlink_to(target)
                    else:
                        os.link(target, raw_path)
                with self.assertRaises((ValueError, RuntimeError)):
                    store.verify_success(query)


if __name__ == "__main__":
    unittest.main()
