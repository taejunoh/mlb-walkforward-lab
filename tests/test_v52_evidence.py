from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

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
    repository = base / "repo"
    repository.mkdir()
    legacy_root = repository / "data/v5_1_bt/raw"
    legacy_query = {
        "end": "2022-04-08",
        "gameType": "R",
        "role": "backtest",
        "start": "2022-04-07",
        "url": "https://statsapi.mlb.com/api/v1/schedule",
    }
    artifact = RawStore(legacy_root).put(
        "mlb_schedule", legacy_query, b"legacy-one\nlegacy-two\n"
    )
    inventory = build_protected_raw_inventory(repository, legacy_root)
    inventory_path = repository / "audit/v5_1_bt/protected_raw_inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_bytes(_canonical_file(inventory))
    record = inventory["records"][0]
    manifest = json.loads(artifact.manifest_path.read_bytes())
    reference = {
        "canonical_query": record["canonical_query"],
        "legacy_manifest_path": record["manifest"]["path"],
        "legacy_manifest_sha256": record["manifest"]["sha256"],
        "legacy_seal_path": record["seal"]["path"],
        "legacy_seal_sha256": record["seal"]["sha256"],
        "parser_version": "schedule-v1",
        "payload_path": record["raw"]["path"],
        "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json",
        "protected_inventory_sha256": hashlib.sha256(
            inventory_path.read_bytes()
        ).hexdigest(),
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
        "source": "mlb_schedule",
    }
    evidence_root = repository / "data/v5_2_qd/historical/raw"
    evidence_root.parent.mkdir(parents=True)
    return V52EvidenceStore(evidence_root), _query(), reference


class V52EvidenceTest(unittest.TestCase):
    def test_acquire_query_has_exact_context_manager_return_annotation(self) -> None:
        annotation = inspect.signature(V52EvidenceStore.acquire_query).return_annotation
        self.assertEqual(annotation, "ContextManager[QueryLease]")

    def test_public_records_are_frozen_with_exact_fields(self) -> None:
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
            ),
            PublishedEvidence: (
                "query_id",
                "success_receipt_path",
                "raw_sha256",
                "query_sidecar_sha256",
                "storage_mode",
            ),
        }
        for record, fields in expected.items():
            with self.subTest(record=record.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(record)), fields
                )
                self.assertTrue(record.__dataclass_params__.frozen)

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
            self.assertEqual(
                (folder / "query.json").read_bytes(),
                canonical_query_bytes(query) + b"\n",
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
            "byte_count": lambda item: item.__setitem__("byte_count", 999),
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
            self.assertEqual(success["query_sha256"], logical_query_id(query))

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
            extra = {**reference, "unreviewed_alias": "forbidden"}
            bad_semantics = {**reference, "semantic_sha256": "0" * 64}
            for label, candidate in (
                ("missing", missing),
                ("extra", extra),
                ("semantic", bad_semantics),
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
                    with patch(
                        "mlb_backtest.v52_evidence._before_success_publish",
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
                    with patch(
                        "mlb_backtest.v52_evidence._before_success_publish",
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
            from mlb_backtest import v52_evidence

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

            def replace_query_directory() -> None:
                folder.rename(moved)
                folder.mkdir()

            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "query|identity|binding"
            ):
                with store.acquire_query(query) as lease:
                    number = store.next_attempt_number(lease)
                    with patch(
                        "mlb_backtest.v52_evidence._before_success_publish",
                        side_effect=replace_query_directory,
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
                (ValueError, RuntimeError), "ambiguous|lock|membership"
            ):
                with second_store.acquire_query(query):
                    self.fail("replacement lock must not create a second valid lease")

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
