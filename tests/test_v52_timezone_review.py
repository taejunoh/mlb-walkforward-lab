from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
RUN_SHA = hashlib.sha256(RUN_SPEC.read_bytes()).hexdigest()
CONTRACT_SHA = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
CANDIDATE_SCHEMA = ROOT / "schemas/v5_2_timezone_candidate.schema.json"
MAPPING_SCHEMA = ROOT / "schemas/v5_2_venue_timezones.schema.json"
REVIEW_SCHEMA = ROOT / "schemas/v5_2_timezone_review_receipt.schema.json"
SEAL_SCHEMA = ROOT / "schemas/v5_2_timezone_seal_receipt.schema.json"
PRODUCTION_MAPPING = ROOT / "config/v5_2_qd/venue_timezones.json"
FORBIDDEN_MAPPING = ROOT / "config/v5_2_qd_venue_timezones.json"
AUDIT_NAMESPACE = Path("audit/v5_2_qd/historical/acquisition")
PROVENANCE = {
    "query_batch_ids": ["c" * 64, "d" * 64],
    "run_spec_sha256": "a" * 64,
    "acquisition_contract_sha256": "b" * 64,
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _sidecar(source: str, grain: str, semantics: dict[str, object], token: str):
    return {
        "byte_count": 1,
        "grain": grain,
        "parser_version": "v5.2.1",
        "query_id": token * 64,
        "raw_sha256": token * 64,
        "schema_version": 1,
        "semantics": semantics,
        "source": source,
    }


def _inputs():
    schedule = _sidecar(
        "mlb_schedule_game_feed",
        "season_schedule_and_game",
        {
            "anchors": [
                {
                    "alternate_site": False,
                    "game_pk": 9001,
                    "neutral_site": False,
                    "official_date": "2024-04-02",
                    "played": True,
                    "venue_id": 31,
                },
                {
                    "alternate_site": True,
                    "game_pk": 9002,
                    "neutral_site": True,
                    "official_date": "2024-04-02",
                    "played": False,
                    "venue_id": 99,
                },
            ]
        },
        "a",
    )
    venues = _sidecar(
        "venue_team_timezone_table",
        "effective_dated_venue_team_reference",
        {
            "venues": [
                {
                    "city": "Home City",
                    "latitude": 40.0,
                    "longitude": -75.0,
                    "venue_id": 31,
                    "venue_name": "Primary Park",
                },
                {
                    "city": "Neutral City",
                    "latitude": 34.0,
                    "longitude": -118.0,
                    "venue_id": 99,
                    "venue_name": "Neutral Field",
                },
            ]
        },
        "b",
    )
    return [schedule], [venues]


def _write(root: Path, relative: Path, value: object) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    return path


def _mapping():
    return json.loads((FIXTURES / "venue_timezones_mini.json").read_bytes())


def _resolve_candidate(candidate: dict[str, object]):
    mapping_by_venue = {
        row["venue_id"]: row for row in _mapping()["timezone_rows"]
    }
    for row in candidate["timezone_rows"]:
        resolved = mapping_by_venue[row["venue_id"]]
        row["effective_start"] = resolved["effective_start"]
        row["effective_end"] = resolved["effective_end"]
        row["time_zone"] = resolved["time_zone"]
    return candidate


def _sealed_candidate_fixture(root: Path, evidence_root: Path):
    from mlb_backtest.protocols import load_run_spec
    from mlb_backtest.v52_evidence import (
        AttemptCapture,
        V52EvidenceStore,
    )
    from mlb_backtest.v52_query_plan import (
        build_schedule_reference_batch,
        build_static_batch,
        publish_query_batch,
    )
    from mlb_backtest.v52_schedule_inventory import (
        publish_schedule_anchor_inventory,
    )
    from mlb_backtest.v52_sources.schedule import (
        recompute_schedule_anchor_sidecar,
    )
    from tests.test_v52_schedule_inventory import (
        _schedule_raw as one_game_schedule_raw,
    )
    from tests.test_v52_source_schedule import (
        _schedule_raw as two_game_schedule_raw,
    )

    protocol_dir = root / "protocols/v5_2_qd"
    protocol_dir.mkdir(parents=True)
    run_spec_path = protocol_dir / RUN_SPEC.name
    contract_path = protocol_dir / CONTRACT.name
    run_spec_path.write_bytes(RUN_SPEC.read_bytes())
    contract_path.write_bytes(CONTRACT.read_bytes())
    run_spec = load_run_spec(run_spec_path)
    schedule_batch = build_static_batch(RUN_SPEC, CONTRACT)
    schedule_queries = schedule_batch.queries
    schedule = schedule_queries[-1]
    publish_query_batch(root, schedule_batch)
    inventory_evidence_root = root / str(run_spec.raw["namespaces"]["raw"])
    inventory_evidence_root.mkdir(parents=True)
    inventory_store = V52EvidenceStore(inventory_evidence_root)
    for query in schedule_queries:
        if int(query.requested_range["year"]) == 2024:
            raw = two_game_schedule_raw()
        else:
            payload = json.loads(one_game_schedule_raw(query, 0))
            game = payload["dates"][0]["games"][0]
            game["gamePk"] = int(query.requested_range["year"]) * 10 + 1
            game["venue"] = {"id": 31, "name": "Primary Park"}
            raw = _canonical(payload)
        sidecar = recompute_schedule_anchor_sidecar(raw, query)
        inventory_capture = AttemptCapture(
            started_at_utc="2026-07-14T00:00:00.000000Z",
            ended_at_utc="2026-07-14T00:00:01.000000Z",
            final_url=query.endpoint,
            http_status=200,
            response_headers=(
                ("content-type", "application/json"),
                ("content-length", str(len(raw))),
            ),
            body=raw,
            error_classification=None,
        )
        with inventory_store.acquire_query(query) as lease:
            inventory_store.publish_fetched(
                query,
                inventory_store.next_attempt_number(lease),
                inventory_capture,
                sidecar["semantics"],
            )
    publish_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=contract_path,
        static_batch_id=schedule_batch.batch_id,
        evidence_root=inventory_evidence_root,
    )
    reference_batch = build_schedule_reference_batch(
        root,
        run_spec_path,
        acquisition_contract_path=contract_path,
        static_batch_id=schedule_batch.batch_id,
        evidence_root=inventory_evidence_root,
    )
    venues = tuple(
        query
        for query in reference_batch.queries
        if query.endpoint.startswith("https://statsapi.mlb.com/api/v1/venues/")
    )
    if len(venues) != 2:
        raise AssertionError("production reference fixture must contain two venues")
    publish_query_batch(root, reference_batch)
    store = V52EvidenceStore(evidence_root)
    capture = AttemptCapture(
        started_at_utc="2026-07-14T00:00:00.000000Z",
        ended_at_utc="2026-07-14T00:00:01.000000Z",
        final_url="https://fixture.invalid",
        http_status=200,
        response_headers=(("Content-Type", "application/json"),),
        body=b"{}",
        error_classification=None,
    )
    schedule_semantics = _inputs()[0][0]["semantics"]
    venue_records = {
        row["venue_id"]: row
        for row in _inputs()[1][0]["semantics"]["venues"]
    }
    evidence = [
        (
            query,
            schedule_semantics
            if query is schedule
            else {
                "anchors": [
                    {
                        "alternate_site": False,
                        "game_pk": int(query.requested_range["year"]) * 10 + 1,
                        "neutral_site": False,
                        "official_date": (
                            f'{int(query.requested_range["year"]):04d}-04-02'
                        ),
                        "played": True,
                        "venue_id": 31,
                    }
                ]
            },
        )
        for query in schedule_queries
    ]
    evidence.extend(
        (query, {"venues": [venue_records[query.requested_range["venue_id"]]]})
        for query in venues
    )
    for query, semantics in evidence:
        with store.acquire_query(query) as lease:
            store.publish_fetched(
                query,
                store.next_attempt_number(lease),
                capture,
                semantics,
            )
    return (schedule_batch, reference_batch), schedule, venues[0]


def _verified_candidate_fixture(
    root: Path,
    *,
    evidence_root: Path | None = None,
    batches=None,
):
    from mlb_backtest.v52_timezone_review import build_timezone_candidate

    if evidence_root is None:
        evidence_root = root / "evidence"
    evidence_root.mkdir(exist_ok=True)
    if batches is None:
        batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
    candidate = build_timezone_candidate(
        root=root,
        evidence_root=evidence_root,
        batch_ids=[batch.batch_id for batch in batches],
    )
    return candidate, batches, evidence_root


def _resolved_verified_candidate_fixture(
    root: Path,
    *,
    evidence_root: Path | None = None,
    batches=None,
):
    candidate, batches, evidence_root = _verified_candidate_fixture(
        root,
        evidence_root=evidence_root,
        batches=batches,
    )
    return _resolve_candidate(candidate), batches, evidence_root


def _reviewed_candidate_fixture(
    root: Path,
    *,
    evidence_root: Path | None = None,
    batches=None,
):
    from mlb_backtest.v52_timezone_review import write_timezone_review_receipt

    candidate = AUDIT_NAMESPACE / "resolved.json"
    resolved, _, _ = _resolved_verified_candidate_fixture(
        root,
        evidence_root=evidence_root,
        batches=batches,
    )
    _write(root, candidate, resolved)
    reviews = []
    for leaf, identity, role in (
        ("semantic.json", "reviewer-a", "TIMEZONE_SEMANTIC_REVIEW"),
        ("coverage.json", "reviewer-b", "TIMEZONE_COVERAGE_REVIEW"),
    ):
        reviews.append(
            write_timezone_review_receipt(
                root,
                candidate_path=candidate,
                output=AUDIT_NAMESPACE / "reviews" / leaf,
                reviewer_identity=identity,
                reviewer_role=role,
                decision="APPROVED",
            ).relative_to(root)
        )
    return candidate, reviews


class V52TimezoneReviewTest(unittest.TestCase):
    def test_public_candidate_builder_requires_verified_sealed_batches(self) -> None:
        from mlb_backtest.v52_timezone_review import build_timezone_candidate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            candidate = build_timezone_candidate(
                root=root,
                evidence_root=evidence_root,
                batch_ids=[batch.batch_id for batch in batches],
            )
            self.assertEqual(
                candidate["query_batch_ids"],
                sorted(batch.batch_id for batch in batches),
            )
            resolved = _resolve_candidate(copy.deepcopy(candidate))
            self.assertEqual(
                {key: value for key, value in resolved.items() if key != "timezone_rows"},
                {key: value for key, value in candidate.items() if key != "timezone_rows"},
            )
            with self.assertRaises(TypeError):
                build_timezone_candidate(*_inputs(), **PROVENANCE)

    def test_candidate_covers_exact_observed_scope_without_guessing(self) -> None:
        from mlb_backtest.v52_timezone_review import _project_timezone_candidate

        schedules, venues = _inputs()
        candidate = _project_timezone_candidate(schedules, venues, **PROVENANCE)
        jsonschema.validate(candidate, json.loads(CANDIDATE_SCHEMA.read_bytes()))
        self.assertEqual(candidate["venue_scope"], [31, 99])
        self.assertEqual(
            candidate["schedule_sidecar_sha256s"],
            [hashlib.sha256(_canonical(schedules[0])).hexdigest()],
        )
        self.assertEqual(
            candidate["venue_sidecar_sha256s"],
            [hashlib.sha256(_canonical(venues[0])).hexdigest()],
        )
        self.assertEqual(
            [row["venue_id"] for row in candidate["timezone_rows"]],
            [31, 99],
        )
        self.assertTrue(
            all(
                row["time_zone"] is None
                and row["review_status"] == "UNREVIEWED"
                for row in candidate["timezone_rows"]
            )
        )
        self.assertEqual(
            candidate["observed_venue_dates"][1],
            {
                "alternate_site": True,
                "neutral_site": True,
                "official_date": "2024-04-02",
                "venue_id": 99,
            },
        )

    def test_candidate_rejects_missing_extra_conflicting_or_unverified_identity(self):
        from mlb_backtest.v52_timezone_review import _project_timezone_candidate

        schedules, venues = _inputs()
        cases = []
        missing = copy.deepcopy(venues)
        missing[0]["semantics"]["venues"].pop()
        cases.append((missing, "missing"))
        extra = copy.deepcopy(venues)
        extra[0]["semantics"]["venues"].append(
            {
                "city": "Extra",
                "latitude": 1.0,
                "longitude": 1.0,
                "venue_id": 777,
                "venue_name": "Extra Park",
            }
        )
        cases.append((extra, "extra"))
        conflict = copy.deepcopy(venues)
        conflict.append(copy.deepcopy(conflict[0]))
        conflict[1]["semantics"]["venues"][0]["venue_name"] = "Wrong"
        conflict[1]["query_id"] = "c" * 64
        cases.append((conflict, "conflict"))
        malformed = copy.deepcopy(venues)
        malformed[0]["unexpected"] = True
        cases.append((malformed, "sidecar"))
        for bad, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                _project_timezone_candidate(schedules, bad, **PROVENANCE)

        conflicting_schedule = copy.deepcopy(schedules)
        conflicting_schedule.append(copy.deepcopy(schedules[0]))
        conflicting_schedule[1]["query_id"] = "d" * 64
        conflicting_schedule[1]["semantics"]["anchors"][0][
            "neutral_site"
        ] = True
        with self.assertRaisesRegex(ValueError, "conflict"):
            _project_timezone_candidate(conflicting_schedule, venues, **PROVENANCE)

    def test_candidate_requires_closed_provenance_and_rejects_same_value_duplicates(self):
        from mlb_backtest.v52_timezone_review import _project_timezone_candidate

        schedules, venues = _inputs()
        candidate = _project_timezone_candidate(
            schedules,
            venues,
            query_batch_ids=["c" * 64, "d" * 64],
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        self.assertEqual(candidate["query_batch_ids"], ["c" * 64, "d" * 64])
        self.assertEqual(candidate["run_spec_sha256"], "a" * 64)
        self.assertEqual(candidate["acquisition_contract_sha256"], "b" * 64)
        jsonschema.validate(candidate, json.loads(CANDIDATE_SCHEMA.read_bytes()))

        duplicate_schedule = copy.deepcopy(schedules[0])
        duplicate_schedule["query_id"] = "e" * 64
        duplicate_schedule["raw_sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "duplicate.*game|game.*duplicate"):
            _project_timezone_candidate(
                [schedules[0], duplicate_schedule],
                venues,
                query_batch_ids=["c" * 64, "d" * 64],
                run_spec_sha256="a" * 64,
                acquisition_contract_sha256="b" * 64,
            )

        duplicate_venue = copy.deepcopy(venues[0])
        duplicate_venue["query_id"] = "f" * 64
        duplicate_venue["raw_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "duplicate.*venue|venue.*duplicate"):
            _project_timezone_candidate(
                schedules,
                [venues[0], duplicate_venue],
                query_batch_ids=["c" * 64, "d" * 64],
                run_spec_sha256="a" * 64,
                acquisition_contract_sha256="b" * 64,
            )

    def test_mapping_fixture_is_valid_closed_and_covers_every_observation(self):
        from mlb_backtest.v52_timezone_review import validate_timezone_mapping

        mapping = _mapping()
        validate_timezone_mapping(mapping, _inputs()[0])
        jsonschema.validate(mapping, json.loads(MAPPING_SCHEMA.read_bytes()))

    def test_mapping_rejects_invalid_zone_range_overlap_gap_and_extra_scope(self):
        from mlb_backtest.v52_timezone_review import validate_timezone_mapping

        mapping = _mapping()
        cases = []
        invalid_zone = copy.deepcopy(mapping)
        invalid_zone["timezone_rows"][0]["time_zone"] = "Mars/Baseball"
        cases.append((invalid_zone, "IANA"))
        reversed_range = copy.deepcopy(mapping)
        reversed_range["timezone_rows"][0]["effective_start"] = "2025-01-01"
        cases.append((reversed_range, "reversed"))
        overlap = copy.deepcopy(mapping)
        overlap["timezone_rows"].append(copy.deepcopy(overlap["timezone_rows"][0]))
        cases.append((overlap, "overlap"))
        gap = copy.deepcopy(mapping)
        gap["timezone_rows"].pop()
        cases.append((gap, "exactly one"))
        extra = copy.deepcopy(mapping)
        extra["timezone_rows"].append(
            {
                "effective_end": "2024-12-31",
                "effective_start": "2024-01-01",
                "time_zone": "UTC",
                "venue_id": 777,
            }
        )
        cases.append((extra, "extra"))
        for bad, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                validate_timezone_mapping(bad, _inputs()[0])

    def test_review_receipt_is_immutable_closed_and_binds_exact_scope(self) -> None:
        from mlb_backtest.v52_timezone_review import (
            write_timezone_review_receipt,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            resolved, _, _ = _resolved_verified_candidate_fixture(root)
            candidate_path = _write(root, candidate_relative, resolved)
            output = AUDIT_NAMESPACE / "reviews/semantic.json"
            path = write_timezone_review_receipt(
                root,
                candidate_path=candidate_relative,
                output=output,
                reviewer_identity="reviewer-a",
                reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                review_scope=[31, 99],
                decision="APPROVED",
            )
            receipt = json.loads(path.read_bytes())
            jsonschema.validate(receipt, json.loads(REVIEW_SCHEMA.read_bytes()))
            self.assertEqual(
                receipt["candidate"],
                {
                    "path": candidate_relative.as_posix(),
                    "sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                },
            )
            self.assertRegex(
                receipt["occurred_at_utc"],
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$",
            )
            self.assertEqual(path.read_bytes(), _canonical(receipt))
            with self.assertRaises(FileExistsError):
                write_timezone_review_receipt(
                    root,
                    candidate_path=candidate_relative,
                    output=output,
                    reviewer_identity="reviewer-a",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    review_scope=[31, 99],
                    decision="APPROVED",
                )

    def test_review_writer_cleans_up_partial_write_and_fsync_failure(self) -> None:
        from mlb_backtest.v52_timezone_review import (
            write_timezone_review_receipt,
        )

        for failure in ("partial_write", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                candidate = AUDIT_NAMESPACE / "candidate.json"
                resolved, _, _ = _resolved_verified_candidate_fixture(root)
                _write(root, candidate, resolved)
                (root / AUDIT_NAMESPACE / "reviews").mkdir(parents=True)
                output = AUDIT_NAMESPACE / f"reviews/{failure}.json"
                if failure == "partial_write":
                    original_write = os.write
                    calls = 0

                    def fail_write(descriptor: int, contents: object) -> int:
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            return original_write(descriptor, bytes(contents[:8]))
                        raise OSError("injected partial write failure")

                    context = patch(
                        "mlb_backtest.v52_timezone_review.os.write",
                        side_effect=fail_write,
                    )
                else:
                    original_fsync = os.fsync

                    def fail_file_fsync(descriptor: int) -> None:
                        metadata = os.fstat(descriptor)
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_size:
                            raise OSError("injected receipt fsync failure")
                        original_fsync(descriptor)

                    context = patch(
                        "mlb_backtest.v52_timezone_review.os.fsync",
                        side_effect=fail_file_fsync,
                    )
                with context, self.assertRaises(OSError):
                    write_timezone_review_receipt(
                        root,
                        candidate_path=candidate,
                        output=output,
                        reviewer_identity="reviewer-a",
                        reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                        review_scope=[31, 99],
                        decision="APPROVED",
                    )
                self.assertFalse((root / output).exists())

    def test_review_writer_rejects_partial_scope_traversal_symlink_and_hardlink(self):
        from mlb_backtest.v52_timezone_review import write_timezone_review_receipt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            unresolved_value, _, _ = _verified_candidate_fixture(root)
            resolved_value = _resolve_candidate(copy.deepcopy(unresolved_value))
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            candidate_path = _write(root, candidate_relative, resolved_value)
            kwargs = {
                "candidate_path": candidate_relative,
                "reviewer_identity": "reviewer-a",
                "reviewer_role": "TIMEZONE_SEMANTIC_REVIEW",
                "decision": "APPROVED",
            }
            with self.assertRaisesRegex(ValueError, "scope"):
                write_timezone_review_receipt(
                    root,
                    output=AUDIT_NAMESPACE / "partial.json",
                    review_scope=[31],
                    **kwargs,
                )
            unresolved = AUDIT_NAMESPACE / "unresolved.json"
            _write(root, unresolved, unresolved_value)
            with self.assertRaisesRegex(ValueError, "unresolved|time_zone"):
                write_timezone_review_receipt(
                    root,
                    candidate_path=unresolved,
                    output=AUDIT_NAMESPACE / "unresolved-review.json",
                    review_scope=[31, 99],
                    **{key: value for key, value in kwargs.items() if key != "candidate_path"},
                )
            with self.assertRaisesRegex(ValueError, "role"):
                write_timezone_review_receipt(
                    root,
                    output=AUDIT_NAMESPACE / "bad-role.json",
                    review_scope=[31, 99],
                    **{**kwargs, "reviewer_role": "fast-worker"},
                )
            with self.assertRaisesRegex(ValueError, "path|root"):
                write_timezone_review_receipt(
                    root,
                    output=Path("../escaped.json"),
                    review_scope=[31, 99],
                    **kwargs,
                )
            alias = root / AUDIT_NAMESPACE / "alias.json"
            alias.symlink_to(candidate_path)
            with self.assertRaisesRegex(ValueError, "no-follow|regular|link"):
                write_timezone_review_receipt(
                    root,
                    candidate_path=AUDIT_NAMESPACE / "alias.json",
                    output=AUDIT_NAMESPACE / "symlink.json",
                    review_scope=[31, 99],
                    **{key: value for key, value in kwargs.items() if key != "candidate_path"},
                )
            hardlink = root / AUDIT_NAMESPACE / "hardlink.json"
            os.link(candidate_path, hardlink)
            with self.assertRaisesRegex(ValueError, "link|regular"):
                write_timezone_review_receipt(
                    root,
                    candidate_path=AUDIT_NAMESPACE / "hardlink.json",
                    output=AUDIT_NAMESPACE / "hardlink-review.json",
                    review_scope=[31, 99],
                    **{key: value for key, value in kwargs.items() if key != "candidate_path"},
                )

    def test_review_writer_revalidates_candidate_venue_and_observation_semantics(self):
        from mlb_backtest.v52_timezone_review import write_timezone_review_receipt

        cases = (
            ("malformed-venue", "coordinate|numeric|latitude"),
            ("duplicate-observation", "duplicate|observation"),
        )
        for index, (mutation, message) in enumerate(cases):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                candidate, _, _ = _resolved_verified_candidate_fixture(root)
                if mutation == "malformed-venue":
                    candidate["venues"][0]["latitude"] = "north"
                else:
                    candidate["observed_venue_dates"].insert(
                        1, copy.deepcopy(candidate["observed_venue_dates"][0])
                    )
                candidate_path = AUDIT_NAMESPACE / "candidate.json"
                _write(root, candidate_path, candidate)
                with self.assertRaisesRegex(ValueError, message):
                    write_timezone_review_receipt(
                        root,
                        candidate_path=candidate_path,
                        output=AUDIT_NAMESPACE / f"review-{index}.json",
                        reviewer_identity="reviewer-a",
                        reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                        review_scope=[31, 99],
                        decision="APPROVED",
                    )

    def test_seal_requires_two_independent_approved_roles_and_binds_evidence(self):
        from mlb_backtest.v52_timezone_review import (
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )

        self.assertFalse(PRODUCTION_MAPPING.exists())
        self.assertFalse(FORBIDDEN_MAPPING.exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            resolved, _, _ = _resolved_verified_candidate_fixture(root)
            _write(root, candidate_relative, resolved)
            reviews = []
            for name, identity, role in (
                ("semantic", "reviewer-a", "TIMEZONE_SEMANTIC_REVIEW"),
                ("coverage", "reviewer-b", "TIMEZONE_COVERAGE_REVIEW"),
            ):
                reviews.append(
                    write_timezone_review_receipt(
                        root,
                        candidate_path=candidate_relative,
                        output=AUDIT_NAMESPACE / f"reviews/{name}.json",
                        reviewer_identity=identity,
                        reviewer_role=role,
                        review_scope=[31, 99],
                        decision="APPROVED",
                    ).relative_to(root)
                )
            mapping_path, seal_path = seal_timezone_mapping(
                root,
                candidate_path=candidate_relative,
                review_receipt_paths=reviews,
                output=Path("config/v5_2_qd/venue_timezones.json"),
                receipt_output=AUDIT_NAMESPACE / "timezone-seal.json",
            )
            mapping = json.loads(mapping_path.read_bytes())
            seal = json.loads(seal_path.read_bytes())
            jsonschema.validate(mapping, json.loads(MAPPING_SCHEMA.read_bytes()))
            jsonschema.validate(seal, json.loads(SEAL_SCHEMA.read_bytes()))
            self.assertEqual(
                seal["mapping"],
                {
                    "path": "config/v5_2_qd/venue_timezones.json",
                    "sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
                },
            )
            self.assertEqual(
                [row["reviewer_role"] for row in seal["reviews"]],
                ["TIMEZONE_COVERAGE_REVIEW", "TIMEZONE_SEMANTIC_REVIEW"],
            )
            self.assertEqual(seal["venue_scope"], [31, 99])
        self.assertFalse(PRODUCTION_MAPPING.exists())
        self.assertFalse(FORBIDDEN_MAPPING.exists())

    def test_seal_rejects_duplicate_identity_role_rejection_and_candidate_mutation(self):
        from mlb_backtest.v52_timezone_review import (
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )

        cases = (
            ("duplicate identity", "reviewer-a", "TIMEZONE_COVERAGE_REVIEW", "APPROVED", False),
            ("duplicate role", "reviewer-b", "TIMEZONE_SEMANTIC_REVIEW", "APPROVED", False),
            ("rejected", "reviewer-b", "TIMEZONE_COVERAGE_REVIEW", "REJECTED", False),
            ("candidate", "reviewer-b", "TIMEZONE_COVERAGE_REVIEW", "APPROVED", True),
        )
        for label, second_identity, second_role, decision, mutate in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                candidate_relative = AUDIT_NAMESPACE / "candidate.json"
                resolved, _, _ = _resolved_verified_candidate_fixture(root)
                candidate_path = _write(
                    root, candidate_relative, resolved
                )
                first = write_timezone_review_receipt(
                    root,
                    candidate_path=candidate_relative,
                    output=AUDIT_NAMESPACE / "reviews/first.json",
                    reviewer_identity="reviewer-a",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    review_scope=[31, 99],
                    decision="APPROVED",
                ).relative_to(root)
                second = write_timezone_review_receipt(
                    root,
                    candidate_path=candidate_relative,
                    output=AUDIT_NAMESPACE / "reviews/second.json",
                    reviewer_identity=second_identity,
                    reviewer_role=second_role,
                    review_scope=[31, 99],
                    decision=decision,
                ).relative_to(root)
                if mutate:
                    candidate_path.write_bytes(candidate_path.read_bytes() + b" ")
                with self.assertRaisesRegex(
                    ValueError, "identity|role|APPROVED|candidate|hash"
                ):
                    seal_timezone_mapping(
                        root,
                        candidate_path=candidate_relative,
                        review_receipt_paths=[first, second],
                        output=Path("config/v5_2_qd/venue_timezones.json"),
                        receipt_output=AUDIT_NAMESPACE / "seal.json",
                    )

    def test_candidate_cli_reads_only_verified_queries_from_sealed_batches(self):
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            output = AUDIT_NAMESPACE / "timezone-candidate.json"
            self.assertEqual(
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batches[0].batch_id,
                        "--batch-id",
                        batches[1].batch_id,
                        "--output",
                        output.as_posix(),
                    ]
                ),
                0,
            )
            candidate = json.loads((root / output).read_bytes())
            jsonschema.validate(candidate, json.loads(CANDIDATE_SCHEMA.read_bytes()))
            self.assertEqual(candidate["venue_scope"], [31, 99])
            self.assertEqual(
                candidate["query_batch_ids"],
                sorted(batch.batch_id for batch in batches),
            )
            self.assertEqual(candidate["run_spec_sha256"], RUN_SHA)
            self.assertEqual(candidate["acquisition_contract_sha256"], CONTRACT_SHA)
            with self.assertRaises(FileExistsError):
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batches[0].batch_id,
                        "--batch-id",
                        batches[1].batch_id,
                        "--output",
                        output.as_posix(),
                    ]
                )

    def test_candidate_cli_rejects_duplicate_batch_and_unpublished_query(self):
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, venue = _sealed_candidate_fixture(root, evidence_root)
            duplicate = [
                "candidate",
                "--root",
                str(root),
                "--evidence-root",
                str(evidence_root),
                "--batch-id",
                batches[0].batch_id,
                "--batch-id",
                batches[0].batch_id,
                "--output",
                (AUDIT_NAMESPACE / "duplicate.json").as_posix(),
            ]
            with self.assertRaisesRegex(ValueError, "duplicate.*batch|batch.*duplicate"):
                main(duplicate)

            success = V52EvidenceStore(evidence_root).read_verified_success(venue)
            success.published_evidence.success_receipt_path.unlink()
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "missing|unpublished|success|query"
            ):
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batches[0].batch_id,
                        "--batch-id",
                        batches[1].batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "missing.json").as_posix(),
                    ]
                )

    def test_candidate_cli_ignores_non_timezone_queries_in_sealed_batch(self):
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            d1_queries = [
                query
                for query in batches[1].queries
                if query.source == "mlb_stats_pitching"
            ]
            self.assertEqual(len(d1_queries), 7)
            d1_2024 = next(
                query
                for query in d1_queries
                if query.requested_range["season"] == 2024
            )
            self.assertEqual(
                d1_2024.parameters,
                {
                    "stats": "byDateRange",
                    "group": "pitching",
                    "sportIds": 1,
                    "gameType": "R",
                    "playerPool": "ALL",
                    "limit": 5000,
                    "startDate": "2024-03-20",
                    "endDate": "2024-04-01",
                },
            )
            self.assertEqual(
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batches[0].batch_id,
                        "--batch-id",
                        batches[1].batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "mixed.json").as_posix(),
                    ]
                ),
                0,
            )

    def test_candidate_cli_ignores_duplicate_unrelated_query_across_batches(self):
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            bisect_statcast_query,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            base, _, venue = _sealed_candidate_fixture(root, evidence_root)
            statcast_root = build_statcast_root_batch(RUN_SPEC, CONTRACT)
            unrelated = bisect_statcast_query(statcast_root.queries[0])[0]
            schedule_batch = base[0]
            venue_batch = base[1]
            remaining_batch = _make_batch("REMAINING", "4" * 64, [unrelated])
            other_remaining_batch = _make_batch("REMAINING", "5" * 64, [unrelated])
            publish_query_batch(root, remaining_batch)
            publish_query_batch(root, other_remaining_batch)
            self.assertEqual(
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        schedule_batch.batch_id,
                        "--batch-id",
                        venue_batch.batch_id,
                        "--batch-id",
                        remaining_batch.batch_id,
                        "--batch-id",
                        other_remaining_batch.batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "split-batches.json").as_posix(),
                    ]
                ),
                0,
            )
            duplicate_relevant = _make_batch(
                "REFERENCE_INPUTS",
                "1" * 64,
                venue_batch.queries,
            )
            with self.assertRaisesRegex(
                ValueError, "REFERENCE_INPUTS|fixed schedule inventory"
            ):
                publish_query_batch(root, duplicate_relevant)

    def test_candidate_cli_rejects_wrong_stage_and_mixed_relevant_binding(self):
        from mlb_backtest.v52_query_plan import _make_batch, publish_query_batch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, schedule, venue = _sealed_candidate_fixture(root, evidence_root)
            wrong_stage = _make_batch(
                "REFERENCE_INPUTS", "2" * 64, batches[1].queries
            )
            with self.assertRaisesRegex(
                ValueError, "REFERENCE_INPUTS|fixed schedule inventory"
            ):
                publish_query_batch(root, wrong_stage)

            mismatched_reference = tuple(
                replace(query, run_spec_sha256="9" * 64)
                for query in batches[1].queries
            )
            mixed = _make_batch(
                "REFERENCE_INPUTS", "3" * 64, mismatched_reference
            )
            with self.assertRaisesRegex(
                ValueError, "binding|run|contract|REFERENCE_INPUTS"
            ):
                publish_query_batch(root, mixed)

    def test_review_and_seal_cli_use_exact_namespaces_and_candidate_scope(self):
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = AUDIT_NAMESPACE / "resolved-candidate.json"
            resolved, _, _ = _resolved_verified_candidate_fixture(root)
            _write(root, candidate, resolved)
            reviews = []
            for name, identity, role in (
                ("semantic", "reviewer-a", "TIMEZONE_SEMANTIC_REVIEW"),
                ("coverage", "reviewer-b", "TIMEZONE_COVERAGE_REVIEW"),
            ):
                output = AUDIT_NAMESPACE / "reviews" / f"{name}.json"
                self.assertEqual(
                    main(
                        [
                            "review",
                            "--root",
                            str(root),
                            "--candidate",
                            candidate.as_posix(),
                            "--output",
                            output.as_posix(),
                            "--reviewer-identity",
                            identity,
                            "--reviewer-role",
                            role,
                            "--decision",
                            "APPROVED",
                        ]
                    ),
                    0,
                )
                reviews.append(output)
            seal_receipt = AUDIT_NAMESPACE / "timezone-seal.json"
            self.assertEqual(
                main(
                    [
                        "seal",
                        "--root",
                        str(root),
                        "--candidate",
                        candidate.as_posix(),
                        "--review-receipt",
                        reviews[0].as_posix(),
                        "--review-receipt",
                        reviews[1].as_posix(),
                        "--output",
                        "config/v5_2_qd/venue_timezones.json",
                        "--receipt-output",
                        seal_receipt.as_posix(),
                    ]
                ),
                0,
            )
            self.assertTrue((root / "config/v5_2_qd/venue_timezones.json").is_file())
            self.assertTrue((root / seal_receipt).is_file())

    def test_public_writers_reject_paths_outside_acquisition_namespaces(self):
        from mlb_backtest.v52_timezone_review import (
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resolved, _, _ = _resolved_verified_candidate_fixture(root)
            bad_candidate = Path("audit/arbitrary.json")
            _write(root, bad_candidate, resolved)
            with self.assertRaisesRegex(ValueError, "namespace|acquisition"):
                write_timezone_review_receipt(
                    root,
                    candidate_path=bad_candidate,
                    output=Path("audit/review.json"),
                    reviewer_identity="reviewer-a",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    review_scope=[31, 99],
                    decision="APPROVED",
                )
            candidate = AUDIT_NAMESPACE / "candidate.json"
            _write(root, candidate, copy.deepcopy(resolved))
            with self.assertRaisesRegex(ValueError, "namespace|acquisition"):
                write_timezone_review_receipt(
                    root,
                    candidate_path=candidate,
                    output=Path("audit/review.json"),
                    reviewer_identity="reviewer-a",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    decision="APPROVED",
                )
            with self.assertRaisesRegex(ValueError, "production|config|namespace"):
                seal_timezone_mapping(
                    root,
                    candidate_path=bad_candidate,
                    review_receipt_paths=[Path("audit/a.json"), Path("audit/b.json")],
                    output=Path("config/v5_2_qd_venue_timezones.json"),
                    receipt_output=Path("audit/seal.json"),
                )
            with self.assertRaisesRegex(ValueError, "namespace|acquisition"):
                seal_timezone_mapping(
                    root,
                    candidate_path=candidate,
                    review_receipt_paths=[Path("audit/a.json"), Path("audit/b.json")],
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=AUDIT_NAMESPACE / "seal.json",
                )
            with self.assertRaisesRegex(ValueError, "namespace|acquisition"):
                seal_timezone_mapping(
                    root,
                    candidate_path=candidate,
                    review_receipt_paths=[
                        AUDIT_NAMESPACE / "reviews/a.json",
                        AUDIT_NAMESPACE / "reviews/b.json",
                    ],
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=Path("audit/seal.json"),
                )

    def test_public_candidate_review_and_seal_reject_symlink_root(self) -> None:
        from mlb_backtest.v52_timezone_review import (
            main,
            seal_timezone_mapping,
            write_timezone_review_receipt,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "real"
            root.mkdir()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            candidate, reviews = _reviewed_candidate_fixture(
                root,
                evidence_root=evidence_root,
                batches=batches,
            )
            alias = base / "root-alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root|symlink"):
                main(
                    [
                        "candidate",
                        "--root",
                        str(alias),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batches[0].batch_id,
                        "--batch-id",
                        batches[1].batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "symlink-candidate.json").as_posix(),
                    ]
                )
            with self.assertRaisesRegex(ValueError, "root|symlink"):
                write_timezone_review_receipt(
                    alias,
                    candidate_path=candidate,
                    output=AUDIT_NAMESPACE / "symlink-review.json",
                    reviewer_identity="reviewer-c",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    decision="APPROVED",
                )
            with self.assertRaisesRegex(ValueError, "root|symlink"):
                seal_timezone_mapping(
                    alias,
                    candidate_path=candidate,
                    review_receipt_paths=reviews,
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=AUDIT_NAMESPACE / "symlink-seal.json",
                )

    def test_write_safe_parent_fsync_failure_preserves_replacement(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            relative = AUDIT_NAMESPACE / "replacement.json"
            path = root / relative
            path.parent.mkdir(parents=True)
            replacement = b"replacement\n"
            original_fsync = os.fsync
            replaced = False

            def fail_parent_fsync(descriptor: int) -> None:
                nonlocal replaced
                metadata = os.fstat(descriptor)
                if stat.S_ISDIR(metadata.st_mode) and path.exists() and not replaced:
                    replaced = True
                    path.unlink()
                    path.write_bytes(replacement)
                    raise OSError("injected parent fsync failure")
                original_fsync(descriptor)

            with (
                patch.object(timezone_review.os, "fsync", side_effect=fail_parent_fsync),
                self.assertRaises(OSError),
            ):
                timezone_review._write_safe(
                    root, relative, b"created\n", "replacement test"
                )
            self.assertEqual(path.read_bytes(), replacement)

    def test_seal_recovers_exact_mapping_left_before_receipt(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate, reviews = _reviewed_candidate_fixture(root)
            mapping = root / "config/v5_2_qd/venue_timezones.json"

            def interrupt() -> None:
                raise KeyboardInterrupt("injected after mapping")

            with (
                patch.object(
                    timezone_review,
                    "_after_mapping_publish_before_seal",
                    side_effect=interrupt,
                    create=True,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                timezone_review.seal_timezone_mapping(
                    root,
                    candidate_path=candidate,
                    review_receipt_paths=reviews,
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=AUDIT_NAMESPACE / "seal.json",
                )
            self.assertTrue(mapping.is_file())
            original_mapping = mapping.read_bytes()
            mapping.write_bytes(original_mapping + b" ")
            with self.assertRaisesRegex(RuntimeError, "immutable.*mapping|mapping.*differs"):
                timezone_review.seal_timezone_mapping(
                    root,
                    candidate_path=candidate,
                    review_receipt_paths=reviews,
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=AUDIT_NAMESPACE / "seal.json",
                )
            self.assertFalse((root / AUDIT_NAMESPACE / "seal.json").exists())
            mapping.write_bytes(original_mapping)
            mapping_path, seal_path = timezone_review.seal_timezone_mapping(
                root,
                candidate_path=candidate,
                review_receipt_paths=reviews,
                output=Path("config/v5_2_qd/venue_timezones.json"),
                receipt_output=AUDIT_NAMESPACE / "seal.json",
            )
            self.assertEqual(mapping_path.read_bytes(), original_mapping)
            self.assertTrue(seal_path.is_file())
            original_seal = seal_path.read_bytes()
            retry_mapping, retry_seal = timezone_review.seal_timezone_mapping(
                root,
                candidate_path=candidate,
                review_receipt_paths=reviews,
                output=Path("config/v5_2_qd/venue_timezones.json"),
                receipt_output=AUDIT_NAMESPACE / "seal.json",
            )
            self.assertEqual(retry_mapping.read_bytes(), original_mapping)
            self.assertEqual(retry_seal.read_bytes(), original_seal)

    def test_seal_rejects_mapping_change_after_safe_write_returns(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review

        for mutation in ("bytes", "same-byte-inode"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                candidate, reviews = _reviewed_candidate_fixture(root)
                mapping = root / "config/v5_2_qd/venue_timezones.json"
                seal = root / AUDIT_NAMESPACE / "seal.json"
                original_write_safe = timezone_review._write_safe

                def write_then_change(*args, **kwargs):
                    result = original_write_safe(*args, **kwargs)
                    published = result[0] if isinstance(result, tuple) else result
                    if args[3] == "timezone mapping":
                        contents = published.read_bytes()
                        if mutation == "bytes":
                            published.write_bytes(contents + b" ")
                        else:
                            replacement = published.with_suffix(".replacement")
                            replacement.write_bytes(contents)
                            os.replace(replacement, published)
                    return result

                with (
                    patch.object(
                        timezone_review,
                        "_write_safe",
                        side_effect=write_then_change,
                    ),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    timezone_review.seal_timezone_mapping(
                        root,
                        candidate_path=candidate,
                        review_receipt_paths=reviews,
                        output=Path("config/v5_2_qd/venue_timezones.json"),
                        receipt_output=AUDIT_NAMESPACE / "seal.json",
                    )
                self.assertTrue(mapping.is_file())
                self.assertFalse(seal.exists())

    def test_review_and_seal_reject_input_replacement_at_publish_boundary(self):
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate, reviews = _reviewed_candidate_fixture(root)
            candidate_path = root / candidate

            def replace_candidate() -> None:
                contents = candidate_path.read_bytes()
                replacement = candidate_path.with_suffix(".replacement")
                replacement.write_bytes(contents)
                os.replace(replacement, candidate_path)

            review_output = AUDIT_NAMESPACE / "boundary-review.json"
            with (
                patch.object(
                    timezone_review,
                    "_before_review_final_rebind",
                    side_effect=replace_candidate,
                    create=True,
                ),
                self.assertRaisesRegex(ValueError, "candidate|identity|changed"),
            ):
                timezone_review.write_timezone_review_receipt(
                    root,
                    candidate_path=candidate,
                    output=review_output,
                    reviewer_identity="reviewer-c",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    decision="APPROVED",
                )
            self.assertFalse((root / review_output).exists())

        for label, target_index in (("candidate", None), ("review-0", 0), ("review-1", 1)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                candidate, reviews = _reviewed_candidate_fixture(root)
                target = root / (candidate if target_index is None else reviews[target_index])

                def replace_input() -> None:
                    contents = target.read_bytes()
                    replacement = target.with_suffix(".replacement")
                    replacement.write_bytes(contents)
                    os.replace(replacement, target)

                seal_output = AUDIT_NAMESPACE / "boundary-seal.json"
                with (
                    patch.object(
                        timezone_review,
                        "_before_seal_final_rebind",
                        side_effect=replace_input,
                        create=True,
                    ),
                    self.assertRaisesRegex(
                        ValueError, "candidate|review|identity|changed"
                    ),
                ):
                    timezone_review.seal_timezone_mapping(
                        root,
                        candidate_path=candidate,
                        review_receipt_paths=reviews,
                        output=Path("config/v5_2_qd/venue_timezones.json"),
                        receipt_output=seal_output,
                    )
                self.assertFalse((root / seal_output).exists())

    def test_cli_help_lists_all_offline_subcommands(self) -> None:
        from mlb_backtest.v52_timezone_review import main

        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaisesRegex(SystemExit, "0"):
            main(["--help"])
        for command in ("candidate", "review", "seal"):
            self.assertIn(command, output.getvalue())


if __name__ == "__main__":
    unittest.main()
