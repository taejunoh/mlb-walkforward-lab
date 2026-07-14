from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
CANDIDATE_SCHEMA = ROOT / "schemas/v5_2_timezone_candidate.schema.json"
MAPPING_SCHEMA = ROOT / "schemas/v5_2_venue_timezones.schema.json"
REVIEW_SCHEMA = ROOT / "schemas/v5_2_timezone_review_receipt.schema.json"
SEAL_SCHEMA = ROOT / "schemas/v5_2_timezone_seal_receipt.schema.json"
PRODUCTION_MAPPING = ROOT / "config/v5_2_qd/venue_timezones.json"
FORBIDDEN_MAPPING = ROOT / "config/v5_2_qd_venue_timezones.json"
AUDIT_NAMESPACE = Path("audit/v5_2_qd/historical/acquisition")


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


def _candidate():
    from mlb_backtest.v52_timezone_review import build_timezone_candidate

    return build_timezone_candidate(*_inputs())


def _mapping():
    return json.loads((FIXTURES / "venue_timezones_mini.json").read_bytes())


def _resolved_candidate():
    candidate = _candidate()
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
    from mlb_backtest.v52_evidence import (
        AttemptCapture,
        LogicalQuery,
        V52EvidenceStore,
    )
    from mlb_backtest.v52_query_plan import _make_batch, publish_query_batch

    common = {
        "expected_media_type": "application/json",
        "parser_version": "v5.2.1",
        "parent_query_id": None,
        "run_spec_sha256": "a" * 64,
        "acquisition_contract_sha256": "b" * 64,
    }
    schedule = LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={"fixture": True},
        requested_range={"year": 2024},
        **common,
    )
    venues = tuple(
        LogicalQuery(
            source="venue_team_timezone_table",
            grain="effective_dated_venue_team_reference",
            endpoint=f"https://statsapi.mlb.com/api/v1/venues/{venue_id}",
            parameters={"hydrate": "location"},
            requested_range={"venue_id": venue_id},
            **common,
        )
        for venue_id in (31, 99)
    )
    batch = _make_batch(
        "TIMEZONE_CANDIDATE_INPUTS", None, [schedule, *venues]
    )
    publish_query_batch(root, batch)
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
    evidence = [(schedule, schedule_semantics)]
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
    return batch, schedule, venues[0]


class V52TimezoneReviewTest(unittest.TestCase):
    def test_candidate_covers_exact_observed_scope_without_guessing(self) -> None:
        from mlb_backtest.v52_timezone_review import build_timezone_candidate

        schedules, venues = _inputs()
        candidate = build_timezone_candidate(schedules, venues)
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
        from mlb_backtest.v52_timezone_review import build_timezone_candidate

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
                build_timezone_candidate(schedules, bad)

        conflicting_schedule = copy.deepcopy(schedules)
        conflicting_schedule.append(copy.deepcopy(schedules[0]))
        conflicting_schedule[1]["query_id"] = "d" * 64
        conflicting_schedule[1]["semantics"]["anchors"][0][
            "neutral_site"
        ] = True
        with self.assertRaisesRegex(ValueError, "conflict"):
            build_timezone_candidate(conflicting_schedule, venues)

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
            root = Path(directory)
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            candidate_path = _write(root, candidate_relative, _resolved_candidate())
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
                root = Path(directory)
                candidate = AUDIT_NAMESPACE / "candidate.json"
                _write(root, candidate, _resolved_candidate())
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
            root = Path(directory)
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            candidate_path = _write(root, candidate_relative, _resolved_candidate())
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
            _write(root, unresolved, _candidate())
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

        cases = []
        malformed_venue = _resolved_candidate()
        malformed_venue["venues"][0]["latitude"] = "north"
        cases.append((malformed_venue, "coordinate|numeric|latitude"))
        duplicate_observation = _resolved_candidate()
        duplicate_observation["observed_venue_dates"].insert(
            1, copy.deepcopy(duplicate_observation["observed_venue_dates"][0])
        )
        cases.append((duplicate_observation, "duplicate|observation"))
        for index, (candidate, message) in enumerate(cases):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
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
            root = Path(directory)
            candidate_relative = AUDIT_NAMESPACE / "candidate.json"
            _write(root, candidate_relative, _resolved_candidate())
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
                root = Path(directory)
                candidate_relative = AUDIT_NAMESPACE / "candidate.json"
                candidate_path = _write(
                    root, candidate_relative, _resolved_candidate()
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
            batch, _, _ = _sealed_candidate_fixture(root, evidence_root)
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
                        batch.batch_id,
                        "--output",
                        output.as_posix(),
                    ]
                ),
                0,
            )
            candidate = json.loads((root / output).read_bytes())
            jsonschema.validate(candidate, json.loads(CANDIDATE_SCHEMA.read_bytes()))
            self.assertEqual(candidate["venue_scope"], [31, 99])
            with self.assertRaises(FileExistsError):
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        batch.batch_id,
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
            batch, _, venue = _sealed_candidate_fixture(root, evidence_root)
            duplicate = [
                "candidate",
                "--root",
                str(root),
                "--evidence-root",
                str(evidence_root),
                "--batch-id",
                batch.batch_id,
                "--batch-id",
                batch.batch_id,
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
                        batch.batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "missing.json").as_posix(),
                    ]
                )

    def test_candidate_cli_ignores_non_timezone_queries_in_sealed_batch(self):
        from mlb_backtest.v52_evidence import LogicalQuery
        from mlb_backtest.v52_query_plan import _make_batch, publish_query_batch
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batch, _, _ = _sealed_candidate_fixture(root, evidence_root)
            unrelated = LogicalQuery(
                source="baseball_savant_statcast",
                grain="pitch",
                endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
                parameters={},
                requested_range={"start_date": "2024-04-01", "end_date": "2024-04-02"},
                expected_media_type="text/csv",
                parser_version="v5.2.1",
                parent_query_id=None,
                run_spec_sha256="a" * 64,
                acquisition_contract_sha256="b" * 64,
            )
            mixed = _make_batch(
                "MIXED_TIMEZONE_INPUTS", None, [*batch.queries, unrelated]
            )
            publish_query_batch(root, mixed)
            self.assertEqual(
                main(
                    [
                        "candidate",
                        "--root",
                        str(root),
                        "--evidence-root",
                        str(evidence_root),
                        "--batch-id",
                        mixed.batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "mixed.json").as_posix(),
                    ]
                ),
                0,
            )

    def test_candidate_cli_ignores_duplicate_unrelated_query_across_batches(self):
        from mlb_backtest.v52_evidence import LogicalQuery
        from mlb_backtest.v52_query_plan import _make_batch, publish_query_batch
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            base, schedule, _ = _sealed_candidate_fixture(root, evidence_root)
            venue_queries = [
                query
                for query in base.queries
                if query.source == "venue_team_timezone_table"
            ]
            unrelated = LogicalQuery(
                source="baseball_savant_statcast",
                grain="pitch",
                endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
                parameters={},
                requested_range={"start_date": "2024-04-01", "end_date": "2024-04-02"},
                expected_media_type="text/csv",
                parser_version="v5.2.1",
                parent_query_id=None,
                run_spec_sha256="a" * 64,
                acquisition_contract_sha256="b" * 64,
            )
            schedule_batch = _make_batch(
                "TIMEZONE_SCHEDULE_INPUTS", None, [schedule, unrelated]
            )
            venue_batch = _make_batch(
                "TIMEZONE_VENUE_INPUTS", None, [*venue_queries, unrelated]
            )
            publish_query_batch(root, schedule_batch)
            publish_query_batch(root, venue_batch)
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
                        "--output",
                        (AUDIT_NAMESPACE / "split-batches.json").as_posix(),
                    ]
                ),
                0,
            )
            duplicate_relevant = _make_batch(
                "DUPLICATE_TIMEZONE_SCHEDULE", None, [schedule]
            )
            publish_query_batch(root, duplicate_relevant)
            with self.assertRaisesRegex(ValueError, "query.*duplicate|duplicate.*query"):
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
                        duplicate_relevant.batch_id,
                        "--batch-id",
                        venue_batch.batch_id,
                        "--output",
                        (AUDIT_NAMESPACE / "duplicate-relevant.json").as_posix(),
                    ]
                )

    def test_review_and_seal_cli_use_exact_namespaces_and_candidate_scope(self):
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = AUDIT_NAMESPACE / "resolved-candidate.json"
            _write(root, candidate, _resolved_candidate())
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
            bad_candidate = Path("audit/arbitrary.json")
            _write(root, bad_candidate, _resolved_candidate())
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
            _write(root, candidate, _resolved_candidate())
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

    def test_cli_help_lists_all_offline_subcommands(self) -> None:
        from mlb_backtest.v52_timezone_review import main

        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaisesRegex(SystemExit, "0"):
            main(["--help"])
        for command in ("candidate", "review", "seal"):
            self.assertIn(command, output.getvalue())


if __name__ == "__main__":
    unittest.main()
