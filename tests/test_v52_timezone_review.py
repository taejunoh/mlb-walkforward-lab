from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, timedelta
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
FIXED_CANDIDATE = AUDIT_NAMESPACE / "venue_timezone_candidate.json"
FIXED_COVERAGE_REVIEW = (
    AUDIT_NAMESPACE / "timezone_reviews/coverage.json"
)
FIXED_SEMANTIC_REVIEW = (
    AUDIT_NAMESPACE / "timezone_reviews/semantic.json"
)
FIXED_SEAL = AUDIT_NAMESPACE / "venue_timezone_seal.json"
SOURCE_AUDIT_NAMESPACE = Path(
    "audit/v5_2_qd/historical_superseding_565534/acquisition"
)
SOURCE_RUN_SPEC = Path(
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
SOURCE_CONTRACT = Path(
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
RECOVERY_AUDIT_NAMESPACE = Path(
    "audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition"
)
RECOVERY_RUN_SPEC = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts.json"
)
RECOVERY_CONTRACT = Path(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts.json"
)
PROVENANCE = {
    "query_batch_ids": ["c" * 64, "d" * 64],
    "run_spec_sha256": "a" * 64,
    "acquisition_contract_sha256": "b" * 64,
}


def _fd_count() -> int | None:
    descriptor_directory = Path("/dev/fd")
    if not descriptor_directory.is_dir():
        return None
    return len(list(descriptor_directory.iterdir()))


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


def _sealed_candidate_fixture(
    root: Path,
    evidence_root: Path,
    *,
    tamper_venue_semantics: bool = False,
):
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
    schedule_semantics = _inputs()[0][0]["semantics"]
    venue_records = {
        row["venue_id"]: row
        for row in _inputs()[1][0]["semantics"]["venues"]
    }
    evidence = []
    if evidence_root != inventory_evidence_root:
        evidence.extend(
            (
                query,
                b"{}",
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
        )
    for index, query in enumerate(venues):
        venue = venue_records[query.requested_range["venue_id"]]
        raw = _canonical(
            {
                "venues": [
                    {
                        "id": venue["venue_id"],
                        "location": {
                            "city": venue["city"],
                            "defaultCoordinates": {
                                "latitude": venue["latitude"],
                                "longitude": venue["longitude"],
                            },
                        },
                        "name": venue["venue_name"],
                    }
                ]
            }
        )
        semantic_venue = dict(venue)
        if tamper_venue_semantics and index == 0:
            semantic_venue["venue_name"] = "Sidecar-only tamper"
        evidence.append((query, raw, {"venues": [semantic_venue]}))
    for query, raw, semantics in evidence:
        capture = AttemptCapture(
            started_at_utc="2026-07-14T00:00:00.000000Z",
            ended_at_utc="2026-07-14T00:00:01.000000Z",
            final_url=query.endpoint,
            http_status=200,
            response_headers=(
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
            ),
            body=raw,
            error_classification=None,
        )
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


def _fixed_verified_gate_fixture(
    root: Path, *, tamper_venue_semantics: bool = False
):
    from mlb_backtest.v52_timezone_review import (
        build_timezone_candidate,
        seal_timezone_mapping,
        write_timezone_review_receipt,
    )

    evidence_root = root / "data/v5_2_qd/historical/raw"
    batches, _, _ = _sealed_candidate_fixture(
        root,
        evidence_root,
        tamper_venue_semantics=tamper_venue_semantics,
    )
    candidate = _resolve_candidate(
        dict(
            build_timezone_candidate(
                root=root,
                evidence_root=evidence_root,
                batch_ids=[batch.batch_id for batch in batches],
            )
        )
    )
    _write(root, FIXED_CANDIDATE, candidate)
    reviews = []
    for path, identity, role in (
        (
            FIXED_COVERAGE_REVIEW,
            "reviewer-coverage",
            "TIMEZONE_COVERAGE_REVIEW",
        ),
        (
            FIXED_SEMANTIC_REVIEW,
            "reviewer-semantic",
            "TIMEZONE_SEMANTIC_REVIEW",
        ),
    ):
        reviews.append(
            write_timezone_review_receipt(
                root,
                candidate_path=FIXED_CANDIDATE,
                output=path,
                reviewer_identity=identity,
                reviewer_role=role,
                decision="APPROVED",
            ).relative_to(root)
        )
    seal_timezone_mapping(
        root,
        candidate_path=FIXED_CANDIDATE,
        review_receipt_paths=reviews,
        output=Path("config/v5_2_qd/venue_timezones.json"),
        receipt_output=FIXED_SEAL,
    )
    return batches, evidence_root


def _source_reference_and_checkpoint_fixture(base: Path):
    """Build the real superseding source authority through REFERENCE_INPUTS."""
    from mlb_backtest.execution_registry import publish_acquisition_checkpoint
    from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id
    from mlb_backtest.v52_query_plan import (
        build_schedule_reference_batch,
        build_static_batch,
        publish_query_batch,
    )
    from mlb_backtest.v52_schedule_inventory import (
        publish_schedule_anchor_inventory,
    )
    from mlb_backtest.v52_source_contracts import recompute_query_sidecar
    from tests.test_v52_game_evidence_inventory import (
        _boxscore_raw,
        _capture,
        _pitching_raw,
        _team_raw,
    )
    from tests.test_v52_schedule_inventory import _source_registry_fixture

    fixture = _source_registry_fixture(base)
    root = fixture.root
    source_run_path = root / SOURCE_RUN_SPEC
    static = build_static_batch(source_run_path, fixture.contract_path)
    disposition_query_id = logical_query_id(static.queries[1])
    static_ids = tuple(sorted(logical_query_id(query) for query in static.queries))
    static_checkpoint = publish_acquisition_checkpoint(
        root,
        fixture.run_spec,
        expected_prior_checkpoint_sha256=None,
        stage="STATIC_SCHEDULES",
        query_batch_ids=(static.batch_id,),
        completed_query_ids=static_ids,
        pending_query_ids=(),
        quarantined_query_ids=(),
        split_query_ids=(),
        dispositioned_query_ids=(disposition_query_id,),
    )
    inventory = publish_schedule_anchor_inventory(
        root,
        fixture.run_spec,
        acquisition_contract_path=fixture.contract_path,
        static_batch_id=static.batch_id,
        evidence_root=fixture.evidence_root,
    )
    reference = build_schedule_reference_batch(
        root,
        source_run_path,
        acquisition_contract_path=fixture.contract_path,
        static_batch_id=static.batch_id,
        evidence_root=fixture.evidence_root,
    )
    publish_query_batch(root, reference)

    anchors = {
        int(row["game_pk"]): row
        for row in json.loads(inventory.path.read_bytes())["anchors"]
    }
    store = V52EvidenceStore(fixture.evidence_root)
    for query in reference.queries:
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
        elif "/api/v1/venues/" in query.endpoint:
            venue_id = int(query.requested_range["venue_id"])
            raw = _canonical(
                {
                    "venues": [
                        {
                            "id": venue_id,
                            "location": {
                                "city": f"Venue {venue_id}",
                                "defaultCoordinates": {
                                    "latitude": 40.0,
                                    "longitude": -75.0,
                                },
                            },
                            "name": f"Park {venue_id}",
                        }
                    ]
                }
            )
        else:
            raise AssertionError(f"unexpected reference query: {query.endpoint}")
        semantics = recompute_query_sidecar(query, raw)
        with store.acquire_query(query) as lease:
            store.publish_fetched(
                query,
                store.next_attempt_number(lease),
                _capture(query, raw),
                semantics,
            )

    cumulative_ids = tuple(
        sorted(
            logical_query_id(query)
            for batch in (static, reference)
            for query in batch.queries
        )
    )
    reference_checkpoint = publish_acquisition_checkpoint(
        root,
        fixture.run_spec,
        expected_prior_checkpoint_sha256=static_checkpoint.receipt_sha256,
        stage="REFERENCE_INPUTS",
        query_batch_ids=(static.batch_id, reference.batch_id),
        completed_query_ids=cumulative_ids,
        pending_query_ids=(),
        quarantined_query_ids=(),
        split_query_ids=(),
        dispositioned_query_ids=(disposition_query_id,),
        stage_evidence_receipts=(inventory.path,),
    )
    return fixture, (static, reference), inventory, reference_checkpoint


def _source_verified_gate_fixture(base: Path):
    from mlb_backtest.execution_registry import (
        _using_verified_source_checkpoint,
    )

    fixture, batches, inventory, checkpoint = (
        _source_reference_and_checkpoint_fixture(base)
    )
    with _using_verified_source_checkpoint(fixture.root, fixture.run_spec):
        return _finish_source_verified_gate_fixture(
            fixture, batches, inventory, checkpoint
        )


def _finish_source_verified_gate_fixture(
    fixture, batches, inventory, checkpoint
):
    from mlb_backtest.v52_timezone_review import (
        load_verified_timezone_gate,
        publish_timezone_candidate,
        seal_timezone_mapping,
        write_timezone_review_receipt,
    )

    root = fixture.root
    authority = {
        "run_spec_path": root / SOURCE_RUN_SPEC,
        "acquisition_contract_path": fixture.contract_path,
    }
    candidate_path = publish_timezone_candidate(
        root=root,
        evidence_root=fixture.evidence_root,
        batch_ids=tuple(batch.batch_id for batch in batches),
        **authority,
    )
    candidate = json.loads(candidate_path.read_bytes())
    for row in candidate["timezone_rows"]:
        row.update(
            {
                "effective_start": "2018-01-01",
                "effective_end": "2024-12-31",
                "time_zone": "America/New_York",
            }
        )
    candidate_path.write_bytes(_canonical(candidate))
    reviews = []
    for leaf, identity, role in (
        (
            "coverage.json",
            "source-reviewer-coverage",
            "TIMEZONE_COVERAGE_REVIEW",
        ),
        (
            "semantic.json",
            "source-reviewer-semantic",
            "TIMEZONE_SEMANTIC_REVIEW",
        ),
    ):
        review_path = SOURCE_AUDIT_NAMESPACE / "timezone_reviews" / leaf
        reviews.append(
            write_timezone_review_receipt(
                root,
                candidate_path=candidate_path,
                output=review_path,
                reviewer_identity=identity,
                reviewer_role=role,
                decision="APPROVED",
                **authority,
            ).relative_to(root)
        )
    seal_timezone_mapping(
        root,
        candidate_path=candidate_path,
        review_receipt_paths=reviews,
        output=Path("config/v5_2_qd/venue_timezones.json"),
        receipt_output=SOURCE_AUDIT_NAMESPACE / "venue_timezone_seal.json",
        **authority,
    )
    gate = load_verified_timezone_gate(root, **authority)
    return fixture, batches, inventory, checkpoint, candidate_path, gate


class V52TimezoneReviewTest(unittest.TestCase):
    def test_cli_normalizes_literal_dot_root_for_historical_and_recovery(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review
        from tests.test_v52_query_plan import RecoveryAuthorityTests

        commands = (
            (
                "candidate",
                [
                    "--evidence-root",
                    "data/v5_2_qd/historical/raw",
                    "--batch-id",
                    "a" * 64,
                    "--output",
                    FIXED_CANDIDATE.as_posix(),
                ],
                "publish_timezone_candidate",
            ),
            (
                "review",
                [
                    "--candidate",
                    FIXED_CANDIDATE.as_posix(),
                    "--output",
                    FIXED_COVERAGE_REVIEW.as_posix(),
                    "--reviewer-identity",
                    "historical-reviewer",
                    "--reviewer-role",
                    "TIMEZONE_COVERAGE_REVIEW",
                    "--decision",
                    "APPROVED",
                ],
                "write_timezone_review_receipt",
            ),
            (
                "seal",
                [
                    "--candidate",
                    FIXED_CANDIDATE.as_posix(),
                    "--review-receipt",
                    FIXED_SEMANTIC_REVIEW.as_posix(),
                    "--review-receipt",
                    FIXED_COVERAGE_REVIEW.as_posix(),
                    "--output",
                    "config/v5_2_qd/venue_timezones.json",
                    "--receipt-output",
                    FIXED_SEAL.as_posix(),
                ],
                "seal_timezone_mapping",
            ),
        )

        def exercise(root: Path, extra: list[str]) -> None:
            previous = Path.cwd()
            try:
                os.chdir(root)
                for command, arguments, function in commands:
                    command_arguments = [
                        (
                            (
                                RECOVERY_AUDIT_NAMESPACE
                                / Path(value).relative_to(AUDIT_NAMESPACE)
                            ).as_posix()
                            if extra
                            and value.startswith(AUDIT_NAMESPACE.as_posix())
                            else value
                        )
                        for value in arguments
                    ]
                    with patch.object(
                        timezone_review,
                        function,
                        return_value=(root / "unused", root / "unused"),
                    ) as invoked:
                        self.assertEqual(
                            timezone_review.main(
                                [
                                    command,
                                    "--root",
                                    ".",
                                    *command_arguments,
                                    *extra,
                                ]
                            ),
                            0,
                        )
                    self.assertEqual(invoked.call_args.args[0] if function != "publish_timezone_candidate" else invoked.call_args.kwargs["root"], root)
                    self.assertEqual(
                        invoked.call_args.kwargs["run_spec_path"],
                        root / (RECOVERY_RUN_SPEC if extra else RUN_SPEC.relative_to(ROOT)),
                    )
            finally:
                os.chdir(previous)

        exercise(ROOT, [])
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec_path, contract_path = RecoveryAuthorityTests()._fixture(
                Path(directory)
            )
            exercise(
                root,
                [
                    "--run-spec",
                    run_spec_path.relative_to(root).as_posix(),
                    "--acquisition-contract",
                    contract_path.relative_to(root).as_posix(),
                ],
            )

    def test_recovery_authority_uses_closed_timezone_paths_and_cli_pair(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from tests.test_v52_query_plan import RecoveryAuthorityTests

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec_path, contract_path = RecoveryAuthorityTests()._fixture(
                Path(directory)
            )
            context = _load_static_contracts(run_spec_path, contract_path)
            paths = timezone_review._timezone_paths(context)
            self.assertEqual(paths.acquisition_audit, RECOVERY_AUDIT_NAMESPACE)
            self.assertEqual(paths.candidate, RECOVERY_AUDIT_NAMESPACE / "venue_timezone_candidate.json")
            self.assertEqual(
                paths.schedule_inventory,
                RECOVERY_AUDIT_NAMESPACE / "schedule_anchor_inventory.json",
            )
            self.assertEqual(
                paths.query_batch_root,
                RECOVERY_AUDIT_NAMESPACE / "query_batches",
            )
            with patch.object(
                timezone_review,
                "publish_timezone_candidate",
                return_value=root / paths.candidate,
            ) as publish:
                self.assertEqual(
                    timezone_review.main(
                        [
                            "candidate",
                            "--root",
                            str(root),
                            "--evidence-root",
                            str(context.raw_root),
                            "--batch-id",
                            "a" * 64,
                            "--output",
                            paths.candidate.as_posix(),
                            "--run-spec",
                            run_spec_path.relative_to(root).as_posix(),
                            "--acquisition-contract",
                            contract_path.relative_to(root).as_posix(),
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                publish.call_args.kwargs["run_spec_path"], run_spec_path
            )
            self.assertEqual(
                publish.call_args.kwargs["acquisition_contract_path"], contract_path
            )
            for changed in (
                replace(
                    context,
                    acquisition_audit_root=root / AUDIT_NAMESPACE,
                ),
                replace(
                    context,
                    authority_path=root / SOURCE_RUN_SPEC,
                ),
                replace(
                    context,
                    acquisition_contract_path=root / SOURCE_CONTRACT,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "authority paths"):
                    timezone_review._timezone_paths(changed)
            schedules, venues = _inputs()
            candidate = timezone_review._project_timezone_candidate(
                schedules,
                venues,
                query_batch_ids=["c" * 64, "d" * 64],
                run_spec_sha256=hashlib.sha256(run_spec_path.read_bytes()).hexdigest(),
                acquisition_contract_sha256="a" * 64,
            )
            candidate["authority"] = {
                "acquisition_contract": {
                    "path": RECOVERY_CONTRACT.as_posix(),
                    "sha256": "a" * 64,
                },
                "run_spec": {
                    "path": RECOVERY_RUN_SPEC.as_posix(),
                    "sha256": candidate["run_spec_sha256"],
                },
            }
            jsonschema.validate(
                candidate, json.loads(CANDIDATE_SCHEMA.read_bytes())
            )
            self.assertEqual(
                timezone_review._candidate_object(candidate)["authority"],
                candidate["authority"],
            )
            mapping = {
                "artifact_kind": "VENUE_TIMEZONE_MAPPING",
                "authority": candidate["authority"],
                "schema_version": 1,
                "timezone_rows": [
                    {
                        "effective_end": "2024-12-31",
                        "effective_start": "2018-01-01",
                        "time_zone": "America/New_York",
                        "venue_id": 31,
                    }
                ],
            }
            mapping_validator = jsonschema.Draft202012Validator(
                json.loads(MAPPING_SCHEMA.read_bytes())
            )
            mapping_validator.validate(_mapping())
            mapping_validator.validate(mapping)
            self.assertEqual(
                timezone_review._mapping_object(mapping)["authority"],
                candidate["authority"],
            )
            seal = {
                "authority": {
                    "acquisition_contract": {
                        "path": RECOVERY_CONTRACT.as_posix(),
                        "sha256": "a" * 64,
                    },
                    "run_spec": {
                        "path": RECOVERY_RUN_SPEC.as_posix(),
                        "sha256": hashlib.sha256(run_spec_path.read_bytes()).hexdigest(),
                    },
                },
                "candidate": {"path": paths.candidate.as_posix(), "sha256": "b" * 64},
                "mapping": {"path": "config/v5_2_qd/venue_timezones.json", "sha256": "c" * 64},
                "occurred_at_utc": "2026-07-28T00:00:00.000000Z",
                "receipt_kind": "TIMEZONE_MAPPING_SEAL",
                "reviews": [
                    {"path": paths.semantic_review.as_posix(), "reviewer_identity": "semantic", "reviewer_role": "TIMEZONE_SEMANTIC_REVIEW", "sha256": "e" * 64},
                    {"path": paths.coverage_review.as_posix(), "reviewer_identity": "coverage", "reviewer_role": "TIMEZONE_COVERAGE_REVIEW", "sha256": "d" * 64},
                ],
                "schema_version": 1,
                "venue_scope": [31],
            }
            validator = jsonschema.Draft202012Validator(
                json.loads(SEAL_SCHEMA.read_bytes())
            )
            validator.validate(seal)
            seal["candidate"]["path"] = FIXED_CANDIDATE.as_posix()
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(seal)
            seal["candidate"]["path"] = paths.candidate.as_posix()
            seal["reviews"].reverse()
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(seal)
            seal["reviews"].reverse()
            seal["authority"]["run_spec"]["path"] = SOURCE_RUN_SPEC.as_posix()
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(seal)

    def test_source_gate_public_lifecycle_and_fail_closed_rebinds(self) -> None:
        import shutil

        from mlb_backtest.execution_registry import (
            _using_verified_source_checkpoint,
            load_latest_acquisition_checkpoint,
        )
        from tests.test_v52_schedule_inventory import (
            _memoized_disposition_projection_patch,
            _memoized_registry_git_patch,
        )

        with (
            _memoized_registry_git_patch(),
            _memoized_disposition_projection_patch(),
            tempfile.TemporaryDirectory() as directory,
            ExitStack() as source_scope,
        ):
            base = Path(directory)
            (
                fixture,
                _batches,
                _inventory,
                reference_checkpoint,
                candidate_path,
                gate,
            ) = _source_verified_gate_fixture(base)
            root = fixture.root
            self.assertEqual(
                candidate_path.relative_to(root),
                SOURCE_AUDIT_NAMESPACE / "venue_timezone_candidate.json",
            )
            self.assertEqual(len(gate.reviews), 2)
            self.assertEqual(
                load_latest_acquisition_checkpoint(root, fixture.run_spec),
                reference_checkpoint,
            )
            source_scope.enter_context(
                _using_verified_source_checkpoint(root, fixture.run_spec)
            )

            from mlb_backtest.v52_timezone_review import (
                load_verified_timezone_gate,
            )

            authority = {
                "run_spec_path": root / SOURCE_RUN_SPEC,
                "acquisition_contract_path": fixture.contract_path,
            }
            source_artifacts = (
                SOURCE_AUDIT_NAMESPACE / "venue_timezone_candidate.json",
                SOURCE_AUDIT_NAMESPACE / "timezone_reviews/coverage.json",
                SOURCE_AUDIT_NAMESPACE / "timezone_reviews/semantic.json",
                SOURCE_AUDIT_NAMESPACE / "venue_timezone_seal.json",
            )
            old_artifacts = tuple(
                AUDIT_NAMESPACE
                / relative.relative_to(SOURCE_AUDIT_NAMESPACE)
                for relative in source_artifacts
            )
            for source_relative, old_relative in zip(
                source_artifacts, old_artifacts, strict=True
            ):
                old_path = root / old_relative
                old_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / source_relative, old_path)
            shadow_gate = load_verified_timezone_gate(root, **authority)
            self.assertEqual(shadow_gate.candidate, gate.candidate)

            candidate_bytes = candidate_path.read_bytes()
            candidate_path.unlink()
            with self.assertRaises((FileNotFoundError, RuntimeError, ValueError)):
                load_verified_timezone_gate(root, **authority)
            candidate_path.write_bytes(candidate_bytes)
            for old_relative in old_artifacts:
                (root / old_relative).unlink()

            wrong = json.loads(candidate_bytes)
            wrong["run_spec_sha256"] = "0" * 64
            candidate_path.write_bytes(_canonical(wrong))
            with self.assertRaisesRegex(ValueError, "candidate|authority"):
                load_verified_timezone_gate(root, **authority)
            candidate_path.write_bytes(candidate_bytes)

            fired = False

            def mutate_before_final_rebind() -> None:
                nonlocal fired
                if not fired:
                    fired = True
                    candidate_path.write_bytes(candidate_bytes + b" ")

            with (
                patch(
                    "mlb_backtest.v52_timezone_review."
                    "_before_timezone_gate_final_rebind",
                    side_effect=mutate_before_final_rebind,
                ),
                self.assertRaises((OSError, RuntimeError, ValueError)),
            ):
                load_verified_timezone_gate(root, **authority)
            self.assertTrue(fired)
            candidate_path.write_bytes(candidate_bytes)

    def test_source_review_and_seal_reject_old_audit_namespace(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review

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
        old_audit = ROOT / "audit/v5_2_qd/historical/acquisition"
        with self.assertRaisesRegex(ValueError, "acquisition audit namespace"):
            timezone_review.write_timezone_review_receipt(
                ROOT,
                candidate_path=old_audit / "venue_timezone_candidate.json",
                output=old_audit / "timezone_reviews/coverage.json",
                reviewer_identity="reviewer",
                reviewer_role="TIMEZONE_COVERAGE_REVIEW",
                decision="APPROVED",
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )
        with self.assertRaisesRegex(ValueError, "acquisition audit namespace"):
            timezone_review.seal_timezone_mapping(
                ROOT,
                candidate_path=old_audit / "venue_timezone_candidate.json",
                review_receipt_paths=(
                    old_audit / "timezone_reviews/coverage.json",
                    old_audit / "timezone_reviews/semantic.json",
                ),
                output=ROOT / "config/v5_2_qd/venue_timezones.json",
                receipt_output=old_audit / "venue_timezone_seal.json",
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )

    def test_source_candidate_rejects_wrong_evidence_and_cross_pair(self) -> None:
        from mlb_backtest.v52_timezone_review import build_timezone_candidate

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
        source_raw = ROOT / "data/v5_2_qd/historical_superseding_565534/raw"
        with self.assertRaisesRegex(ValueError, "evidence root"):
            build_timezone_candidate(
                root=ROOT,
                evidence_root=ROOT / "data/v5_2_qd/historical/raw",
                batch_ids=("0" * 64,),
                run_spec_path=source_run,
                acquisition_contract_path=source_contract,
            )
        with self.assertRaises(ValueError):
            build_timezone_candidate(
                root=ROOT,
                evidence_root=source_raw,
                batch_ids=("0" * 64,),
                run_spec_path=source_run,
                acquisition_contract_path=CONTRACT,
            )

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

    def test_review_writer_preserves_partial_write_and_fsync_residue(self) -> None:
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
                        "mlb_backtest.secure_fs.os.write",
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
                        "mlb_backtest.secure_fs.os.fsync",
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
                self.assertTrue((root / output).exists())
                with self.assertRaises(FileExistsError):
                    write_timezone_review_receipt(
                        root,
                        candidate_path=candidate,
                        output=output,
                        reviewer_identity="reviewer-a",
                        reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                        review_scope=[31, 99],
                        decision="APPROVED",
                    )

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
            output = FIXED_CANDIDATE
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
            before = (root / output).stat()
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
            after = (root / output).stat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_public_candidate_publisher_is_idempotent_and_preserves_resolution(self):
        from mlb_backtest.v52_source_contracts import canonical_json_bytes
        from mlb_backtest.v52_timezone_review import publish_timezone_candidate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            kwargs = {
                "root": root,
                "evidence_root": evidence_root,
                "batch_ids": [batch.batch_id for batch in batches],
            }
            path = publish_timezone_candidate(**kwargs)
            first = path.stat()
            self.assertEqual(publish_timezone_candidate(**kwargs), path)
            self.assertEqual((path.stat().st_dev, path.stat().st_ino),
                             (first.st_dev, first.st_ino))

            resolved = json.loads(path.read_bytes())
            resolved["timezone_rows"][0]["time_zone"] = "UTC"
            resolved_bytes = canonical_json_bytes(resolved) + b"\n"
            path.write_bytes(resolved_bytes)
            resolved_stat = path.stat()
            self.assertEqual(publish_timezone_candidate(**kwargs), path)
            self.assertEqual(path.read_bytes(), resolved_bytes)
            self.assertEqual((path.stat().st_dev, path.stat().st_ino),
                             (resolved_stat.st_dev, resolved_stat.st_ino))

            for row in resolved["timezone_rows"]:
                row["time_zone"] = "UTC"
            resolved_bytes = canonical_json_bytes(resolved) + b"\n"
            path.write_bytes(resolved_bytes)
            self.assertEqual(publish_timezone_candidate(**kwargs), path)
            self.assertEqual(path.read_bytes(), resolved_bytes)

            forged = json.loads(path.read_bytes())
            forged["run_spec_sha256"] = "f" * 64
            path.write_bytes(canonical_json_bytes(forged) + b"\n")
            with self.assertRaisesRegex((ValueError, RuntimeError), "authority|differs"):
                publish_timezone_candidate(**kwargs)

    def test_candidate_publisher_allows_unobserved_gaps_but_rejects_uncovered_observations(self):
        from mlb_backtest.v52_source_contracts import canonical_json_bytes
        from mlb_backtest.v52_timezone_review import publish_timezone_candidate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            kwargs = {
                "root": root,
                "evidence_root": evidence_root,
                "batch_ids": [batch.batch_id for batch in batches],
            }
            path = publish_timezone_candidate(**kwargs)
            candidate = json.loads(path.read_bytes())
            for row in candidate["timezone_rows"]:
                row["time_zone"] = "UTC"
            target = next(
                row
                for row in candidate["timezone_rows"]
                if row["effective_start"] < row["effective_end"]
            )
            candidate["timezone_rows"].remove(target)
            split_day = date.fromisoformat(target["effective_start"])
            first = dict(target, effective_end=split_day.isoformat(), time_zone="UTC")
            second = dict(
                target,
                effective_start=(split_day + timedelta(days=1)).isoformat(),
                time_zone="America/New_York",
            )
            candidate["timezone_rows"].extend((first, second))
            candidate["timezone_rows"].sort(
                key=lambda row: (row["venue_id"], row["effective_start"])
            )
            valid_bytes = canonical_json_bytes(candidate) + b"\n"
            path.write_bytes(valid_bytes)
            before = path.stat()

            self.assertEqual(publish_timezone_candidate(**kwargs), path)
            self.assertEqual(path.read_bytes(), valid_bytes)
            self.assertEqual(
                (path.stat().st_dev, path.stat().st_ino),
                (before.st_dev, before.st_ino),
            )

            unobserved_gap = copy.deepcopy(candidate)
            gap_row = next(
                row
                for row in unobserved_gap["timezone_rows"]
                if row["venue_id"] == target["venue_id"]
                and row["effective_start"] == second["effective_start"]
            )
            gap_row["effective_start"] = (
                date.fromisoformat(second["effective_start"]) + timedelta(days=1)
            ).isoformat()
            gap_bytes = canonical_json_bytes(unobserved_gap) + b"\n"
            path.write_bytes(gap_bytes)
            gap_stat = path.stat()
            self.assertEqual(publish_timezone_candidate(**kwargs), path)
            self.assertEqual(path.read_bytes(), gap_bytes)
            self.assertEqual(
                (path.stat().st_dev, path.stat().st_ino),
                (gap_stat.st_dev, gap_stat.st_ino),
            )

            uncovered = copy.deepcopy(candidate)
            uncovered["timezone_rows"].remove(
                next(
                    row
                    for row in uncovered["timezone_rows"]
                    if row["venue_id"] == target["venue_id"]
                    and row["effective_start"] == first["effective_start"]
                )
            )
            path.write_bytes(canonical_json_bytes(uncovered) + b"\n")
            with self.assertRaisesRegex(ValueError, "coverage|resolve"):
                publish_timezone_candidate(**kwargs)

            overlap = copy.deepcopy(candidate)
            overlap_row = next(
                row
                for row in overlap["timezone_rows"]
                if row["venue_id"] == target["venue_id"]
                and row["effective_start"] == second["effective_start"]
            )
            overlap_row["effective_start"] = first["effective_end"]
            path.write_bytes(canonical_json_bytes(overlap) + b"\n")
            with self.assertRaisesRegex(ValueError, "overlap"):
                publish_timezone_candidate(**kwargs)

    def test_candidate_publisher_rejects_source_mutation_at_final_rebind(self):
        import mlb_backtest.v52_timezone_review as timezone_review
        from mlb_backtest.v52_evidence import V52EvidenceStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, venue = _sealed_candidate_fixture(root, evidence_root)
            kwargs = {
                "root": root,
                "evidence_root": evidence_root,
                "batch_ids": [batch.batch_id for batch in batches],
            }
            path = timezone_review.publish_timezone_candidate(**kwargs)
            before = path.read_bytes()
            success = V52EvidenceStore(evidence_root).read_verified_success(venue)

            def mutate_source() -> None:
                success.published_evidence.success_receipt_path.unlink()

            with (
                patch.object(
                    timezone_review,
                    "_before_timezone_candidate_final_rebind",
                    side_effect=mutate_source,
                ),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                timezone_review.publish_timezone_candidate(**kwargs)
            self.assertEqual(path.read_bytes(), before)

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
                FIXED_CANDIDATE.as_posix(),
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
                        FIXED_CANDIDATE.as_posix(),
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
                        FIXED_CANDIDATE.as_posix(),
                    ]
                ),
                0,
            )

    def test_candidate_cli_ignores_duplicate_unrelated_query_across_batches(self):
        from mlb_backtest.v52_query_plan import (
            _build_statcast_root_batch,
            _make_batch,
            bisect_statcast_query,
            publish_query_batch,
        )
        from mlb_backtest.v52_timezone_review import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            base, _, venue = _sealed_candidate_fixture(root, evidence_root)
            statcast_root = _build_statcast_root_batch(RUN_SPEC, CONTRACT)
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
                        FIXED_CANDIDATE.as_posix(),
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
                        FIXED_CANDIDATE.as_posix(),
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
        from mlb_backtest import secure_fs

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
                patch.object(secure_fs.os, "fsync", side_effect=fail_parent_fsync),
                self.assertRaises((OSError, ValueError)),
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
                original_write_safe = timezone_review._write_safe_from_trusted

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
                        "_write_safe_from_trusted",
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
            self.assertTrue((root / review_output).exists())

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
                self.assertTrue((root / seal_output).exists())

    def test_verified_gate_loads_only_fixed_current_authority_and_deep_freezes(self):
        from mlb_backtest.v52_timezone_review import load_verified_timezone_gate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            batches, evidence_root = _fixed_verified_gate_fixture(root)
            gate = load_verified_timezone_gate(root)

            self.assertEqual(
                gate.query_batch_ids,
                tuple(batch.batch_id for batch in batches),
            )
            self.assertEqual(
                gate.schedule_inventory_sha256,
                batches[1].derived_from_sha256,
            )
            self.assertEqual(gate.run_spec_sha256, RUN_SHA)
            self.assertEqual(gate.acquisition_contract_sha256, CONTRACT_SHA)
            self.assertEqual(
                gate.mapping_sha256,
                hashlib.sha256(
                    (root / "config/v5_2_qd/venue_timezones.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                [review["reviewer_role"] for review in gate.reviews],
                ["TIMEZONE_COVERAGE_REVIEW", "TIMEZONE_SEMANTIC_REVIEW"],
            )
            self.assertTrue(
                all(
                    row["review_status"] == "UNREVIEWED"
                    and isinstance(row["time_zone"], str)
                    for row in gate.candidate["timezone_rows"]
                )
            )
            with self.assertRaises(TypeError):
                gate.mapping["schema_version"] = 2
            with self.assertRaises(TypeError):
                gate.candidate["timezone_rows"][0]["time_zone"] = "UTC"
            with self.assertRaises(TypeError):
                load_verified_timezone_gate(root, evidence_root=evidence_root)

    def test_verified_gate_replays_venue_raw_instead_of_trusting_sidecar(self):
        from mlb_backtest.v52_timezone_review import load_verified_timezone_gate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixed_verified_gate_fixture(
                root,
                tamper_venue_semantics=True,
            )
            with self.assertRaisesRegex(
                ValueError, "venue|sidecar|raw|semantic"
            ):
                load_verified_timezone_gate(root)

    def test_verified_gate_rebinds_every_fixed_artifact_before_return(self):
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixed_verified_gate_fixture(root)
            candidate = root / FIXED_CANDIDATE

            def replace_candidate() -> None:
                replacement = candidate.with_suffix(".replacement")
                replacement.write_bytes(candidate.read_bytes())
                os.replace(replacement, candidate)

            with (
                patch.object(
                    timezone_review,
                    "_before_timezone_gate_final_rebind",
                    side_effect=replace_candidate,
                    create=True,
                ),
                self.assertRaisesRegex(
                    ValueError, "candidate|identity|changed|rebind"
                ),
            ):
                timezone_review.load_verified_timezone_gate(root)

    def test_verified_gate_rebinds_after_second_authority_replay(self):
        import mlb_backtest.v52_timezone_review as timezone_review

        for label, relative in (
            ("candidate", FIXED_CANDIDATE),
            (
                "authority",
                Path("protocols/v5_2_qd/historical_walkforward_500.json"),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _fixed_verified_gate_fixture(root)
                target = root / relative
                original = timezone_review._current_timezone_authority
                calls = 0

                def replay_then_replace(
                    replay_root: Path,
                    *,
                    read_only_progress: bool = False,
                ):
                    nonlocal calls
                    authority = original(
                        replay_root,
                        read_only_progress=read_only_progress,
                    )
                    calls += 1
                    if calls == 2:
                        replacement = target.with_suffix(".replacement")
                        replacement.write_bytes(target.read_bytes())
                        os.replace(replacement, target)
                    return authority

                with (
                    patch.object(
                        timezone_review,
                        "_current_timezone_authority",
                        side_effect=replay_then_replace,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "candidate|authority|identity|changed|rebind",
                    ),
                ):
                    timezone_review.load_verified_timezone_gate(root)

    def test_verified_gate_rebinds_after_first_authority_replay(self):
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixed_verified_gate_fixture(root)
            target = root / "protocols/v5_2_qd/historical_walkforward_500.json"
            original = timezone_review._current_timezone_authority
            calls = 0

            def replay_then_replace(
                replay_root,
                *,
                read_only_progress: bool = False,
            ):
                nonlocal calls
                authority = original(
                    replay_root,
                    read_only_progress=read_only_progress,
                )
                calls += 1
                if calls == 1:
                    replacement = target.with_suffix(".replacement")
                    replacement.write_bytes(target.read_bytes())
                    os.replace(replacement, target)
                return authority

            with (
                patch.object(
                    timezone_review,
                    "_current_timezone_authority",
                    side_effect=replay_then_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "authority|identity|changed|rebind",
                ),
            ):
                timezone_review.load_verified_timezone_gate(root)

    def test_cli_help_lists_all_offline_subcommands(self) -> None:
        from mlb_backtest.v52_timezone_review import main

        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaisesRegex(SystemExit, "0"):
            main(["--help"])
        for command in ("candidate", "review", "seal"):
            self.assertIn(command, output.getvalue())

    def test_public_operations_reject_bound_root_ancestor_swaps(self) -> None:
        """Every public path operation keeps its one root capability bound."""
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            held = base / "held"
            canary = base / "canary"
            canary.mkdir()
            marker = canary / "marker"
            marker.write_bytes(b"canary\n")
            baseline = _fd_count()
            original_load = timezone_review.load_query_batch
            swapped = False

            def load_then_replace(*args, **kwargs):
                nonlocal swapped
                result = original_load(*args, **kwargs)
                if not swapped:
                    swapped = True
                    root.rename(held)
                    root.mkdir()
                return result

            with (
                patch.object(
                    timezone_review,
                    "load_query_batch",
                    side_effect=load_then_replace,
                ),
                self.assertRaisesRegex(ValueError, "root|unsafe"),
            ):
                timezone_review.publish_timezone_candidate(
                    root=root,
                    evidence_root=held / "evidence",
                    batch_ids=[batch.batch_id for batch in batches],
                )
            self.assertEqual(marker.read_bytes(), b"canary\n")
            self.assertFalse((root / FIXED_CANDIDATE).exists())
            if baseline is not None:
                self.assertEqual(_fd_count(), baseline)

    def test_public_operations_reject_complementary_ancestor_swaps(self) -> None:
        """Complete candidate/review/seal x directory/symlink attack coverage."""
        import mlb_backtest.v52_timezone_review as timezone_review
        from mlb_backtest import secure_fs

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            batches, _, _ = _sealed_candidate_fixture(root, evidence_root)
            held = base / "held"
            canary = base / "canary"
            canary.mkdir()
            marker = canary / "marker"
            marker.write_bytes(b"candidate-canary\n")
            baseline = _fd_count()
            original_load = timezone_review.load_query_batch
            swapped = False

            def load_then_symlink(*args, **kwargs):
                nonlocal swapped
                result = original_load(*args, **kwargs)
                if not swapped:
                    swapped = True
                    root.rename(held)
                    root.symlink_to(canary, target_is_directory=True)
                return result

            with (
                patch.object(
                    timezone_review,
                    "load_query_batch",
                    side_effect=load_then_symlink,
                ),
                self.assertRaisesRegex(ValueError, "root|unsafe"),
            ):
                timezone_review.publish_timezone_candidate(
                    root=root,
                    evidence_root=held / "evidence",
                    batch_ids=[batch.batch_id for batch in batches],
                )
            self.assertEqual(marker.read_bytes(), b"candidate-canary\n")
            self.assertFalse((canary / FIXED_CANDIDATE).exists())
            if baseline is not None:
                self.assertEqual(_fd_count(), baseline)

        for operation in ("review", "seal"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "root"
                root.mkdir()
                candidate, reviews = _reviewed_candidate_fixture(root)
                held = base / "held"
                attacker = base / "attacker"
                output = AUDIT_NAMESPACE / f"directory-{operation}.json"
                baseline = _fd_count()

                def install_attacker() -> None:
                    root.rename(held)
                    root.mkdir()
                    attacker_marker = root / "marker"
                    attacker_marker.write_bytes(b"attacker\n")
                    root.rename(attacker)
                    attacker.rename(root)

                if operation == "review":

                    def replace_root(
                        stage: str, component: str | None = None
                    ) -> None:
                        if (
                            stage == "after_file_fsync"
                            and component == output.as_posix()
                        ):
                            install_attacker()

                    context = patch.object(
                        secure_fs,
                        "_secure_fs_test_hook",
                        side_effect=replace_root,
                    )
                else:
                    context = patch.object(
                        timezone_review,
                        "_after_mapping_publish_before_seal",
                        side_effect=install_attacker,
                    )
                with (
                    context,
                    self.assertRaisesRegex(
                        ValueError, "root|unsafe|identity|changed"
                    ),
                ):
                    if operation == "review":
                        timezone_review.write_timezone_review_receipt(
                            root,
                            candidate_path=candidate,
                            output=output,
                            reviewer_identity="reviewer-c",
                            reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                            decision="APPROVED",
                        )
                    else:
                        timezone_review.seal_timezone_mapping(
                            root,
                            candidate_path=candidate,
                            review_receipt_paths=reviews,
                            output=Path("config/v5_2_qd/venue_timezones.json"),
                            receipt_output=output,
                        )
                self.assertEqual((root / "marker").read_bytes(), b"attacker\n")
                self.assertFalse((root / output).exists())
                if operation == "review":
                    self.assertTrue((held / output).is_file())
                else:
                    self.assertTrue(
                        (held / "config/v5_2_qd/venue_timezones.json").is_file()
                    )
                if baseline is not None:
                    self.assertEqual(_fd_count(), baseline)

    def test_verified_gate_detects_transient_root_swap_at_path_boundary(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            _fixed_verified_gate_fixture(root)
            target = root / "protocols/v5_2_qd/historical_walkforward_500.json"
            held = base / "held"
            canary = base / "canary"
            canary.mkdir()
            marker = canary / "marker"
            marker.write_bytes(b"transient-canary\n")
            original = timezone_review._current_timezone_authority
            calls = 0
            baseline = _fd_count()

            def replay_then_transient_swap(
                replay_root,
                *,
                read_only_progress: bool = False,
            ):
                nonlocal calls
                authority = original(
                    replay_root,
                    read_only_progress=read_only_progress,
                )
                calls += 1
                if calls == 2:
                    root.rename(held)
                    root.symlink_to(canary, target_is_directory=True)
                    root.unlink()
                    held.rename(root)
                    replacement = target.with_suffix(".replacement")
                    replacement.write_bytes(target.read_bytes())
                    os.replace(replacement, target)
                return authority

            with (
                patch.object(
                    timezone_review,
                    "_current_timezone_authority",
                    side_effect=replay_then_transient_swap,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "authority|identity|changed|rebind",
                ),
            ):
                timezone_review.load_verified_timezone_gate(root)
            self.assertEqual(marker.read_bytes(), b"transient-canary\n")
            if baseline is not None:
                self.assertEqual(_fd_count(), baseline)

    def test_review_and_seal_reject_symlink_ancestor_swaps(self) -> None:
        import mlb_backtest.v52_timezone_review as timezone_review
        from mlb_backtest import secure_fs

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            candidate, _ = _reviewed_candidate_fixture(root)
            held = base / "held"
            canary = base / "canary"
            canary.mkdir()
            marker = canary / "marker"
            marker.write_bytes(b"canary\n")
            output = AUDIT_NAMESPACE / "ancestor-review.json"
            baseline = _fd_count()

            def replace_root(stage: str, component: str | None = None) -> None:
                if stage == "after_file_fsync" and component == output.as_posix():
                    root.rename(held)
                    root.symlink_to(canary, target_is_directory=True)

            with (
                patch.object(
                    secure_fs,
                    "_secure_fs_test_hook",
                    side_effect=replace_root,
                ),
                self.assertRaisesRegex(ValueError, "root|unsafe|identity|changed"),
            ):
                timezone_review.write_timezone_review_receipt(
                    root,
                    candidate_path=candidate,
                    output=output,
                    reviewer_identity="reviewer-c",
                    reviewer_role="TIMEZONE_SEMANTIC_REVIEW",
                    decision="APPROVED",
                )
            self.assertEqual(marker.read_bytes(), b"canary\n")
            self.assertFalse((canary / output).exists())
            self.assertTrue((held / output).is_file())
            if baseline is not None:
                self.assertEqual(_fd_count(), baseline)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            root.mkdir()
            candidate, reviews = _reviewed_candidate_fixture(root)
            held = base / "held"
            canary = base / "canary"
            canary.mkdir()
            marker = canary / "marker"
            marker.write_bytes(b"canary\n")
            baseline = _fd_count()

            def replace_after_mapping() -> None:
                root.rename(held)
                root.symlink_to(canary, target_is_directory=True)

            with (
                patch.object(
                    timezone_review,
                    "_after_mapping_publish_before_seal",
                    side_effect=replace_after_mapping,
                ),
                self.assertRaisesRegex(ValueError, "root|unsafe|identity|changed"),
            ):
                timezone_review.seal_timezone_mapping(
                    root,
                    candidate_path=candidate,
                    review_receipt_paths=reviews,
                    output=Path("config/v5_2_qd/venue_timezones.json"),
                    receipt_output=AUDIT_NAMESPACE / "ancestor-seal.json",
                )
            self.assertEqual(marker.read_bytes(), b"canary\n")
            self.assertFalse(
                (canary / "config/v5_2_qd/venue_timezones.json").exists()
            )
            self.assertTrue(
                (held / "config/v5_2_qd/venue_timezones.json").is_file()
            )
            if baseline is not None:
                self.assertEqual(_fd_count(), baseline)


if __name__ == "__main__":
    unittest.main()
