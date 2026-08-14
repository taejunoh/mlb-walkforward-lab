from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch
from urllib.parse import urlencode

from mlb_backtest.manifest import RawStore
from mlb_backtest.v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    canonical_query_bytes,
    logical_query_id,
)
from mlb_backtest.v52_protected_inventory import build_protected_raw_inventory
from mlb_backtest.statcast import statcast_url


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
INVENTORY_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
LEGACY_RELATIVE = Path("data/v5_1_bt/raw")


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


def _schedule_raw() -> bytes:
    payload = json.loads((SOURCE_FIXTURES / "schedule_mini.json").read_bytes())
    payload["totalGames"] = 2
    payload["dates"][0]["totalGames"] = 2
    payload["dates"][0]["games"][0]["neutralSite"] = False
    payload["dates"][0]["games"][0]["isAlternateSite"] = False
    payload["dates"][0]["games"][1]["neutralSite"] = True
    payload["dates"][0]["games"][1]["isAlternateSite"] = True
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _schedule_query() -> LogicalQuery:
    return LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={
            "sportId": 1,
            "gameType": "R",
            "startDate": "2024-03-20",
            "endDate": "2024-09-30",
            "hydrate": "probablePitcher,lineups,venue,linescore",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-03-20",
            "end_date": "2024-09-30",
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _career_query(
    *, inclusive_end: str = "2017-12-31", cutoff: str = "2021-12-31"
) -> LogicalQuery:
    return LogicalQuery(
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": "1876-01-01",
            "endDate": inclusive_end,
        },
        requested_range={
            "pitcher_id": 101,
            "inclusive_end": inclusive_end,
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": cutoff},
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _statcast_query(inventory_sha256: str) -> LogicalQuery:
    first = last = "2024-04-02"
    child = LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={
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
        },
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
            **child.parameters,
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


def _pitching_query() -> LogicalQuery:
    return LogicalQuery(
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint="https://statsapi.mlb.com/api/v1/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "sportIds": 1,
            "gameType": "R",
            "playerPool": "ALL",
            "limit": 5000,
            "startDate": "2024-03-20",
            "endDate": "2024-04-01",
        },
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


def _legacy_schedule_query(*, role: str = "training") -> dict[str, object]:
    parameters = {
        "sportId": 1,
        "gameType": "R",
        "startDate": "2024-03-20",
        "endDate": "2024-09-30",
        "hydrate": "probablePitcher,lineups,venue,linescore",
    }
    return {
        "end": "2024-09-30",
        "gameType": "R",
        "role": role,
        "start": "2024-03-20",
        "url": f"https://statsapi.mlb.com/api/v1/schedule?{urlencode(parameters)}",
    }


def _repository(
    base: Path,
    *,
    raw: bytes | None = None,
    legacy_query: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, str]:
    repository = base / "repository"
    repository.mkdir()
    repository = repository.resolve()
    legacy_root = repository / LEGACY_RELATIVE
    RawStore(legacy_root).put(
        "mlb_schedule",
        legacy_query or _legacy_schedule_query(),
        _schedule_raw() if raw is None else raw,
    )
    inventory = build_protected_raw_inventory(repository, legacy_root)
    inventory_path = repository / INVENTORY_RELATIVE
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_bytes(_canonical_file(inventory))
    digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    return repository, legacy_root, inventory_path, digest


def _load(repository: Path, legacy_root: Path, inventory: Path, digest: str):
    from mlb_backtest.v52_legacy import (
        adapt_root_local_legacy_catalog,
        load_protected_legacy_catalog,
    )

    return adapt_root_local_legacy_catalog(
        load_protected_legacy_catalog(
            repository,
            legacy_root=legacy_root,
            protected_inventory_path=inventory,
            protected_inventory_sha256=digest,
        )
    )


def _store(repository: Path, *, legacy_evidence=None) -> V52EvidenceStore:
    evidence_root = repository / "data/v5_2_qd/historical/raw"
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    return V52EvidenceStore(evidence_root, legacy_evidence=legacy_evidence)


def _repository_records(
    base: Path,
    records: tuple[tuple[str, dict[str, object], bytes], ...],
) -> tuple[Path, Path, Path, str]:
    repository = (base / "repository").resolve()
    repository.mkdir()
    legacy_root = repository / LEGACY_RELATIVE
    for source, legacy_query, raw in records:
        RawStore(legacy_root).put(source, legacy_query, raw)
    inventory = build_protected_raw_inventory(repository, legacy_root)
    inventory_path = repository / INVENTORY_RELATIVE
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_bytes(_canonical_file(inventory))
    return (
        repository,
        legacy_root,
        inventory_path,
        hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
    )


class V52LegacyTest(unittest.TestCase):
    def test_quality_i1_canonical_roundtrip_preserves_nonstatcast_matches(self) -> None:
        cases = (
            (
                "schedule",
                "mlb_schedule",
                _legacy_schedule_query(),
                _schedule_raw(),
                _schedule_query(),
            ),
            (
                "d1",
                "mlb_stats_pitching",
                {
                    **_pitching_query().parameters,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(tuple(_pitching_query().parameters.items()))
                    ),
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                _pitching_query(),
            ),
            (
                "career",
                "mlb_stats_pitcher_bf",
                {
                    **_career_query().parameters,
                    "pitcher_id": 101,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/people/101/stats?"
                        + urlencode(tuple(_career_query().parameters.items()))
                    ),
                },
                b'{"stats":[]}',
                _career_query(),
            ),
        )
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        for label, source, legacy_query, raw, original in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository, legacy_root, inventory, digest = _repository_records(
                    Path(directory), ((source, legacy_query, raw),)
                )
                catalog = _load(repository, legacy_root, inventory, digest)
                round_tripped = LogicalQuery(
                    **json.loads(canonical_query_bytes(original))
                )
                self.assertIs(
                    audit_legacy_candidate(round_tripped, catalog).disposition,
                    PromotionDisposition.PROMOTED,
                )

    def test_quality_i2_audit_and_promotion_never_full_verify_or_read_unrelated(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        query = _schedule_query()
        unrelated = _legacy_schedule_query(role="unrelated")
        unrelated["url"] = str(unrelated["url"]) + "&unused=1"
        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository_records(
                Path(directory),
                (
                    ("mlb_schedule", _legacy_schedule_query(), _schedule_raw()),
                    ("mlb_schedule", unrelated, b"unrelated protected payload\n"),
                ),
            )
            from mlb_backtest import v52_legacy

            original_verifier = v52_legacy.verify_protected_raw_inventory
            with patch.object(
                v52_legacy,
                "verify_protected_raw_inventory",
                wraps=original_verifier,
            ) as verifier:
                catalog = _load(repository, legacy_root, inventory, digest)
            self.assertEqual(verifier.call_count, 1)

            def assert_metadata_only(value: object) -> None:
                self.assertNotIsInstance(value, (bytes, bytearray, memoryview))
                if dataclasses.is_dataclass(value):
                    for field in dataclasses.fields(value):
                        assert_metadata_only(getattr(value, field.name))
                elif isinstance(value, Mapping):
                    for key, item in value.items():
                        assert_metadata_only(key)
                        assert_metadata_only(item)
                elif isinstance(value, (tuple, list, set, frozenset)):
                    for item in value:
                        assert_metadata_only(item)

            assert_metadata_only(catalog)
            inventory_document = json.loads(inventory.read_bytes())
            unrelated_path = repository / next(
                record["raw"]["path"]
                for record in inventory_document["records"]
                if record["canonical_query"].get("role") == "unrelated"
            )
            unrelated_candidate = next(
                candidate
                for candidate in catalog.catalog.candidates
                if candidate.payload_path == unrelated_path
            )
            original_snapshot = v52_legacy._snapshot
            from mlb_backtest import v52_evidence

            self.assertFalse(
                hasattr(v52_evidence, "verify_protected_raw_inventory")
            )

            def reject_unrelated(root: Path, path: Path):
                if path == unrelated_candidate.payload_path:
                    raise AssertionError("audit opened unrelated payload")
                return original_snapshot(root, path)

            with (
                patch(
                    "mlb_backtest.v52_legacy.verify_protected_raw_inventory",
                    side_effect=AssertionError("audit reran full verifier"),
                ),
                patch("mlb_backtest.v52_legacy._snapshot", side_effect=reject_unrelated),
            ):
                decision = audit_legacy_candidate(query, catalog)
                self.assertIs(decision.disposition, PromotionDisposition.PROMOTED)
                published = promote_legacy_candidate(
                    _store(repository, legacy_evidence=catalog), query, catalog, decision
                )
            self.assertEqual(published.storage_mode, "PROTECTED_LEGACY_REFERENCE")

    def test_quality_i3_i4_malformed_current_query_is_closed_fetch(self) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            malformed = dataclasses.replace(
                _schedule_query(),
                requested_range={
                    **_schedule_query().requested_range,
                    "year": [],
                },
            )
            decision = audit_legacy_candidate(malformed, catalog)
            self.assertIs(decision.disposition, PromotionDisposition.FETCH_REQUIRED)
            self.assertEqual(
                decision.reason_codes,
                (PromotionReasonCode.CURRENT_QUERY_INVALID,),
            )

            wrong_parser = dataclasses.replace(
                _schedule_query(), parser_version="v5.2.0"
            )
            parser_decision = audit_legacy_candidate(wrong_parser, catalog)
            self.assertIs(
                parser_decision.disposition, PromotionDisposition.FETCH_REQUIRED
            )
            self.assertEqual(
                parser_decision.reason_codes,
                (PromotionReasonCode.CURRENT_PARSER_CONTRACT_UNSATISFIED,),
            )

    def test_quality_i3_all_json_matcher_fields_have_deterministic_decisions(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        cases = (
            (
                "schedule",
                "mlb_schedule",
                _legacy_schedule_query(),
                _schedule_raw(),
                lambda _digest: _schedule_query(),
            ),
            (
                "d1",
                "mlb_stats_pitching",
                {
                    **_pitching_query().parameters,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(tuple(_pitching_query().parameters.items()))
                    ),
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                lambda _digest: _pitching_query(),
            ),
            (
                "career",
                "mlb_stats_pitcher_bf",
                {
                    **_career_query().parameters,
                    "pitcher_id": 101,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/people/101/stats?"
                        + urlencode(tuple(_career_query().parameters.items()))
                    ),
                },
                b'{"stats":[]}',
                lambda _digest: _career_query(),
            ),
            (
                "statcast",
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes(),
                _statcast_query,
            ),
        )
        malformed_values: tuple[object, ...] = (None, [], {}, "wrong", 7)
        for label, source, legacy_query, raw, query_factory in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository, legacy_root, inventory, digest = _repository_records(
                    Path(directory), ((source, legacy_query, raw),)
                )
                catalog = _load(repository, legacy_root, inventory, digest)
                query = query_factory(digest)
                for field_name in ("parameters", "requested_range"):
                    original = getattr(query, field_name)
                    for key in tuple(original):
                        for malformed in malformed_values:
                            if malformed == original[key]:
                                continue
                            with self.subTest(
                                label=label,
                                field=field_name,
                                key=key,
                                malformed=malformed,
                            ):
                                changed = dataclasses.replace(
                                    query,
                                    **{
                                        field_name: {
                                            **original,
                                            key: malformed,
                                        }
                                    },
                                )
                                decision = audit_legacy_candidate(changed, catalog)
                                self.assertIs(
                                    decision.disposition,
                                    PromotionDisposition.FETCH_REQUIRED,
                                )

    def test_quality_i4_promotion_noneligibility_and_publisher_failures_are_typed(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import ProtectedLegacyPublishReverifyError
        from mlb_backtest.v52_legacy import (
            LegacyPromotionNotEligibleError,
            PromotionReasonCode,
            ProtectedLegacyIntegrityError,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            query = _schedule_query()
            decision = audit_legacy_candidate(query, catalog)
            store = _store(repository)
            with (
                patch(
                    "mlb_backtest.v52_legacy.recompute_query_sidecar",
                    side_effect=ValueError("fresh semantic rejection"),
                ),
                self.assertRaises(LegacyPromotionNotEligibleError) as noneligible,
            ):
                promote_legacy_candidate(store, query, catalog, decision)
            self.assertEqual(
                noneligible.exception.reason_code,
                PromotionReasonCode.CURRENT_SEMANTIC_CONTRACT_UNSATISFIED,
            )

            with (
                patch.object(
                    store,
                    "publish_legacy_reference",
                    side_effect=ProtectedLegacyPublishReverifyError("changed"),
                ),
                self.assertRaises(ProtectedLegacyIntegrityError) as protected,
            ):
                promote_legacy_candidate(store, query, catalog, decision)
            self.assertEqual(
                protected.exception.reason_code,
                PromotionReasonCode.PROTECTED_PUBLISH_REVERIFY_FAILED,
            )

            with patch.object(
                store,
                "publish_legacy_reference",
                side_effect=ValueError("generic publisher rejection"),
            ), self.assertRaisesRegex(ValueError, "generic publisher rejection"):
                promote_legacy_candidate(store, query, catalog, decision)

    def test_consumer_specific_audits_may_reuse_raw_with_distinct_references(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        query = _pitching_query()
        legacy_query = {
            **query.parameters,
            "url": query.endpoint + "?" + urlencode(tuple(query.parameters.items())),
        }
        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository_records(
                Path(directory),
                (
                    (
                        "mlb_stats_pitching",
                        legacy_query,
                        (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                    ),
                ),
            )
            catalog = _load(repository, legacy_root, inventory, digest)
            second = dataclasses.replace(
                query,
                requested_range={
                    **query.requested_range,
                    "consuming_game_pks": [9002],
                    "consumer_cutoff_dates": {"9002": "2024-04-01"},
                },
            )
            first_decision = audit_legacy_candidate(query, catalog)
            second_decision = audit_legacy_candidate(second, catalog)
            self.assertIs(first_decision.disposition, PromotionDisposition.PROMOTED)
            self.assertIs(second_decision.disposition, PromotionDisposition.PROMOTED)
            self.assertEqual(first_decision.candidate, second_decision.candidate)

            store = _store(repository)
            first_published = promote_legacy_candidate(
                store, query, catalog, first_decision
            )
            second_published = promote_legacy_candidate(
                store, second, catalog, second_decision
            )
            self.assertEqual(first_published.raw_sha256, second_published.raw_sha256)
            for current in (query, second):
                folder = (
                    store.root
                    / "queries"
                    / current.source
                    / logical_query_id(current)
                    / "objects"
                )
                reference = json.loads(
                    next(folder.glob("*.legacy-reference.json")).read_bytes()
                )
                self.assertEqual(
                    reference["current_query_id"], logical_query_id(current)
                )

    def test_quality_m1_has_no_exception_message_classifier(self) -> None:
        from mlb_backtest import v52_legacy

        self.assertFalse(hasattr(v52_legacy, "_reason_for_inventory_failure"))

    def test_public_records_are_frozen_and_catalog_is_inventory_bound(self) -> None:
        from mlb_backtest.v52_legacy import (
            LegacyCandidate,
            PromotionDecision,
            PromotionDisposition,
            ProtectedLegacyCatalog,
        )

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(LegacyCandidate)),
            (
                "source",
                "manifest_path",
                "manifest_sha256",
                "seal_path",
                "payload_path",
                "raw_sha256",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(PromotionDecision)),
            ("query_id", "disposition", "reason_codes", "candidate"),
        )
        for record in (LegacyCandidate, ProtectedLegacyCatalog, PromotionDecision):
            self.assertTrue(record.__dataclass_params__.frozen)
        self.assertEqual(
            {item.value for item in PromotionDisposition},
            {"PROMOTED", "FETCH_REQUIRED", "FATAL_INTEGRITY"},
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            self.assertEqual(catalog.catalog.repository_root, repository.resolve())
            self.assertEqual(catalog.catalog.legacy_root, LEGACY_RELATIVE)
            self.assertEqual(
                catalog.catalog.protected_inventory_path, INVENTORY_RELATIVE
            )
            self.assertEqual(catalog.inventory_sha256, digest)
            self.assertEqual(len(catalog.catalog.candidates), 1)
            self.assertEqual(catalog.catalog.candidates[0].source, "mlb_schedule")

    def test_root_local_adapter_preserves_catalog_bytes_and_rebinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            adapter = catalog
            record = adapter.records[0]
            self.assertEqual(record.payload_path, PurePosixPath("data/v5_1_bt/raw/mlb_schedule") / record.payload_path.name)
            self.assertEqual(
                adapter.read_bytes(record.payload_path),
                catalog.catalog.records[0].candidate.payload_path.read_bytes(),
            )
            catalog.catalog.records[0].candidate.payload_path.write_bytes(b"changed")
            with self.assertRaises(Exception):
                adapter.rebind()

    def test_legacy_consumers_reject_unissued_duck_typed_capability(self) -> None:
        from mlb_backtest.v52_legacy import audit_legacy_candidate

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            forged = SimpleNamespace(
                logical_raw_root=PurePosixPath("data/v5_1_bt/raw"),
                inventory_sha256=digest,
                records=(),
            )
            with self.assertRaises(TypeError):
                audit_legacy_candidate(_schedule_query(), forged)

    def test_root_and_external_capabilities_have_equal_audit_and_promotion_outputs(
        self,
    ) -> None:
        """The split-root capability must preserve legacy logical outputs."""
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_legacy import (
            audit_legacy_candidate,
            audit_legacy_candidate_evidence,
            audit_legacy_candidates_evidence,
            promote_legacy_candidate,
        )
        from mlb_backtest.v52_protected_inventory import (
            parse_protected_raw_inventory_document,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = (base / "parent").resolve()
            parent.mkdir()
            parent_raw = parent / LEGACY_RELATIVE
            RawStore(parent_raw).put(
                "mlb_schedule", _legacy_schedule_query(), _schedule_raw()
            )
            inventory = build_protected_raw_inventory(parent, parent_raw)
            inventory_path = parent / INVENTORY_RELATIVE
            inventory_path.parent.mkdir(parents=True)
            inventory_path.write_bytes(_canonical_file(inventory))
            digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
            target = (base / "target").resolve()
            target.mkdir()
            local = _load(parent, parent_raw, inventory_path, digest)
            query = _schedule_query()
            local_decision = audit_legacy_candidate(query, local)
            local_evidence = audit_legacy_candidate_evidence(query, local)
            local_batch_evidence = audit_legacy_candidates_evidence(
                (query,), local
            )
            local_store = _store(parent)
            local_published = promote_legacy_candidate(
                local_store, query, local, local_decision
            )
            for name in (
                ".baseball_savant_statcast.lock",
                ".mlb_stats_pitcher_bf.lock",
                ".mlb_stats_pitching.lock",
            ):
                (parent_raw / name).write_bytes(b"")
            from mlb_backtest.v52_legacy import _snapshot

            target_inventory = target / INVENTORY_RELATIVE
            target_inventory.parent.mkdir(parents=True)
            target_inventory.write_bytes(inventory_path.read_bytes())
            external = _bind_external_inventory(
                target,
                parent_raw,
                parse_protected_raw_inventory_document(inventory_path.read_bytes()),
                digest,
                inventory_path=target_inventory,
                inventory_snapshot=_snapshot(target, target_inventory),
            )
            self.addCleanup(external.close)
            external_decision = audit_legacy_candidate(query, external)
            external_evidence = audit_legacy_candidate_evidence(query, external)
            external_batch_evidence = audit_legacy_candidates_evidence(
                (query,), external
            )
            self.assertEqual(local_decision, external_decision)
            self.assertEqual(local_evidence, external_evidence)
            self.assertEqual(local_batch_evidence, external_batch_evidence)
            external_store = _store(target)
            captured: list[bytes] = []

            def capture_reference(_query, reference, _semantics):
                captured.append(_canonical_file(reference))
                return local_published

            with patch.object(
                external_store,
                "publish_legacy_reference",
                side_effect=capture_reference,
            ):
                external_published = promote_legacy_candidate(
                    external_store, query, external, external_decision
                )
            local_reference = next(
                (local_store.root / "queries").glob(
                    f"{query.source}/{logical_query_id(query)}/objects/*.json"
                )
            ).read_bytes()
            self.assertEqual(local_published, external_published)
            self.assertEqual(local_reference, captured[0])
            self.assertNotIn(os.fspath(parent), captured[0].decode())

    def test_catalog_rejects_wrong_paths_hash_and_uninventoried_membership(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            cases = (
                {"legacy_root": repository / "other"},
                {"protected_inventory_path": repository / "inventory.json"},
                {"protected_inventory_sha256": "0" * 64},
            )
            for overrides in cases:
                kwargs = {
                    "legacy_root": legacy_root,
                    "protected_inventory_path": inventory,
                    "protected_inventory_sha256": digest,
                    **overrides,
                }
                with (
                    self.subTest(overrides=overrides),
                    self.assertRaises(ProtectedLegacyIntegrityError),
                ):
                    from mlb_backtest.v52_legacy import load_protected_legacy_catalog

                    load_protected_legacy_catalog(repository, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            (legacy_root / "mlb_schedule/uninventoried.raw").write_bytes(b"hidden")
            with self.assertRaises(ProtectedLegacyIntegrityError):
                _load(repository, legacy_root, inventory, digest)

    def test_catalog_rejects_inventory_manifest_seal_and_raw_tamper(self) -> None:
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        for target_name in ("inventory", "manifest", "seal", "payload"):
            with (
                self.subTest(target=target_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository, legacy_root, inventory, digest = _repository(
                    Path(directory)
                )
                catalog = _load(repository, legacy_root, inventory, digest)
                target = {
                    "inventory": inventory,
                    "manifest": catalog.catalog.candidates[0].manifest_path,
                    "seal": catalog.catalog.candidates[0].seal_path,
                    "payload": catalog.catalog.candidates[0].payload_path,
                }[target_name]
                contents = target.read_bytes()
                target.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
                with self.assertRaises(ProtectedLegacyIntegrityError):
                    _load(repository, legacy_root, inventory, digest)

    def test_catalog_rejects_symlink_hardlink_and_symlinked_source_ancestor(self):
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        for attack in ("symlink", "hardlink", "ancestor"):
            with (
                self.subTest(attack=attack),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository, legacy_root, inventory, digest = _repository(
                    Path(directory)
                )
                catalog = _load(repository, legacy_root, inventory, digest)
                payload = catalog.catalog.candidates[0].payload_path
                if attack == "symlink":
                    replacement = payload.with_suffix(".saved")
                    payload.rename(replacement)
                    payload.symlink_to(replacement.name)
                elif attack == "hardlink":
                    replacement = payload.with_suffix(".saved")
                    replacement.write_bytes(payload.read_bytes())
                    payload.unlink()
                    os.link(replacement, payload)
                else:
                    source = payload.parent
                    replacement = source.with_name("mlb_schedule.saved")
                    source.rename(replacement)
                    source.symlink_to(replacement.name, target_is_directory=True)
                with self.assertRaises(ProtectedLegacyIntegrityError):
                    _load(repository, legacy_root, inventory, digest)

    def test_catalog_rejects_repository_reached_through_symlink_ancestor(self):
        from mlb_backtest.v52_legacy import (
            ProtectedLegacyIntegrityError,
            load_protected_legacy_catalog,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real = base / "real"
            real.mkdir()
            _repository_path, _legacy_root, _inventory, digest = _repository(real)
            alias = base / "repository-alias"
            alias.symlink_to(real, target_is_directory=True)
            repository = alias / "repository"
            with self.assertRaises(ProtectedLegacyIntegrityError):
                load_protected_legacy_catalog(
                    repository,
                    legacy_root=repository / LEGACY_RELATIVE,
                    protected_inventory_path=repository / INVENTORY_RELATIVE,
                    protected_inventory_sha256=digest,
                )

    def test_schedule_alias_promotes_and_career_does_not_alias_annual_pitching(self):
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            promoted = audit_legacy_candidate(_schedule_query(), catalog)
            self.assertIs(promoted.disposition, PromotionDisposition.PROMOTED)
            self.assertEqual(promoted.reason_codes, ())
            self.assertIsNotNone(promoted.candidate)

            career = audit_legacy_candidate(_career_query(), catalog)
            self.assertIs(career.disposition, PromotionDisposition.FETCH_REQUIRED)
            self.assertIsNone(career.candidate)

            for changed in (
                dataclasses.replace(
                    _schedule_query(),
                    endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
                ),
                dataclasses.replace(
                    _schedule_query(),
                    requested_range={
                        **_schedule_query().requested_range,
                        "end_date": "2024-09-29",
                    },
                ),
                dataclasses.replace(
                    _schedule_query(),
                    parameters={**_schedule_query().parameters, "sportId": 2},
                ),
            ):
                self.assertIs(
                    audit_legacy_candidate(changed, catalog).disposition,
                    PromotionDisposition.FETCH_REQUIRED,
                )

    def test_audit_fetches_on_no_match_current_parser_or_legacy_2021_career(self):
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(
                Path(directory), raw=b"not-current-schedule-json\n"
            )
            decision = audit_legacy_candidate(
                _schedule_query(), _load(repository, legacy_root, inventory, digest)
            )
            self.assertIs(decision.disposition, PromotionDisposition.FETCH_REQUIRED)
            self.assertTrue(decision.reason_codes)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            repository = repository.resolve()
            legacy_root = repository / LEGACY_RELATIVE
            RawStore(legacy_root).put(
                "mlb_stats_pitcher_bf",
                {
                    "endDate": "2021-12-31",
                    "gameType": "R",
                    "group": "pitching",
                    "pitcher_id": 101,
                    "sportIds": 1,
                    "startDate": "1876-01-01",
                    "stats": "byDateRange",
                    "url": "https://statsapi.mlb.com/api/v1/people/101/stats",
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
            )
            value = build_protected_raw_inventory(repository, legacy_root)
            inventory = repository / INVENTORY_RELATIVE
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes(_canonical_file(value))
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            decision = audit_legacy_candidate(
                _career_query(), _load(repository, legacy_root, inventory, digest)
            )
            self.assertIs(decision.disposition, PromotionDisposition.FETCH_REQUIRED)

    def test_statcast_and_d1_require_exact_current_query_contract(self) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        cases = (
            (
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes(),
                _statcast_query,
            ),
            (
                "mlb_stats_pitching",
                {
                    "stats": "byDateRange",
                    "group": "pitching",
                    "sportIds": 1,
                    "gameType": "R",
                    "playerPool": "ALL",
                    "limit": 5000,
                    "startDate": "2024-03-20",
                    "endDate": "2024-04-01",
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(_pitching_query().parameters)
                    ),
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                lambda _digest: _pitching_query(),
            ),
        )
        for source, legacy_query, raw, query_factory in cases:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository = Path(directory) / "repository"
                repository.mkdir()
                repository = repository.resolve()
                legacy_root = repository / LEGACY_RELATIVE
                RawStore(legacy_root).put(source, legacy_query, raw)
                value = build_protected_raw_inventory(repository, legacy_root)
                inventory = repository / INVENTORY_RELATIVE
                inventory.parent.mkdir(parents=True)
                inventory.write_bytes(_canonical_file(value))
                digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
                catalog = _load(repository, legacy_root, inventory, digest)
                query = query_factory(digest)
                self.assertIs(
                    audit_legacy_candidate(query, catalog).disposition,
                    PromotionDisposition.PROMOTED,
                )
                if source == "baseball_savant_statcast":
                    wrong_hash = dataclasses.replace(
                        query,
                        requested_range={
                            **query.requested_range,
                            "protected_inventory_sha256": "0" * 64,
                        },
                    )
                    self.assertIs(
                        audit_legacy_candidate(wrong_hash, catalog).disposition,
                        PromotionDisposition.FETCH_REQUIRED,
                    )

    def test_statcast_promotion_requires_exact_planned_child_parent_lineage(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            repository = repository.resolve()
            legacy_root = repository / LEGACY_RELATIVE
            RawStore(legacy_root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes(),
            )
            value = build_protected_raw_inventory(repository, legacy_root)
            inventory = repository / INVENTORY_RELATIVE
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes(_canonical_file(value))
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            catalog = _load(repository, legacy_root, inventory, digest)

            child = _statcast_query(digest)
            parent = dataclasses.replace(
                child,
                parameters={
                    **child.parameters,
                    "game_date_gt": "2024-04-01",
                    "game_date_lt": "2024-04-30",
                },
                requested_range={
                    "year": 2024,
                    "start_date": "2024-04-01",
                    "end_date": "2024-04-30",
                },
                parent_query_id=None,
            )
            planned = dataclasses.replace(
                child,
                parent_query_id=logical_query_id(parent),
            )
            self.assertIs(
                audit_legacy_candidate(planned, catalog).disposition,
                PromotionDisposition.PROMOTED,
            )

            for parent_query_id in (None, "d" * 64):
                with self.subTest(parent_query_id=parent_query_id):
                    decision = audit_legacy_candidate(
                        dataclasses.replace(
                            planned,
                            parent_query_id=parent_query_id,
                        ),
                        catalog,
                    )
                    self.assertIs(
                        decision.disposition,
                        PromotionDisposition.FETCH_REQUIRED,
                    )
                    self.assertEqual(
                        decision.reason_codes,
                        (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                    )
            structurally_invalid = audit_legacy_candidate(
                dataclasses.replace(planned, parent_query_id="C" * 64),
                catalog,
            )
            self.assertIs(
                structurally_invalid.disposition,
                PromotionDisposition.FETCH_REQUIRED,
            )
            self.assertEqual(structurally_invalid.query_id, "0" * 64)
            self.assertEqual(
                structurally_invalid.reason_codes,
                (PromotionReasonCode.CURRENT_QUERY_INVALID,),
            )

    def test_career_bf_promotes_only_at_exact_current_cutoff_and_never_cross_aliases(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
        )

        fixtures = (
            (
                "mlb_stats_pitcher_bf",
                {
                    "endDate": "2017-12-31",
                    "gameType": "R",
                    "group": "pitching",
                    "pitcher_id": 101,
                    "sportIds": 1,
                    "startDate": "1876-01-01",
                    "stats": "byDateRange",
                    "url": (
                        "https://statsapi.mlb.com/api/v1/people/101/stats?"
                        + urlencode(_career_query().parameters)
                    ),
                },
                _career_query(),
                _pitching_query(),
            ),
            (
                "mlb_stats_pitching",
                {
                    **_pitching_query().parameters,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(_pitching_query().parameters)
                    ),
                },
                _pitching_query(),
                _career_query(),
            ),
        )
        for source, legacy_query, exact_query, cross_query in fixtures:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository = Path(directory) / "repository"
                repository.mkdir()
                repository = repository.resolve()
                legacy_root = repository / LEGACY_RELATIVE
                RawStore(legacy_root).put(
                    source,
                    legacy_query,
                    (
                        b'{"stats":[]}'
                        if source == "mlb_stats_pitcher_bf"
                        else (SOURCE_FIXTURES / "pitching_mini.json").read_bytes()
                    ),
                )
                inventory_value = build_protected_raw_inventory(repository, legacy_root)
                inventory = repository / INVENTORY_RELATIVE
                inventory.parent.mkdir(parents=True)
                inventory.write_bytes(_canonical_file(inventory_value))
                digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
                catalog = _load(repository, legacy_root, inventory, digest)

                exact = audit_legacy_candidate(exact_query, catalog)
                self.assertIs(exact.disposition, PromotionDisposition.PROMOTED)
                self.assertEqual(exact.reason_codes, ())
                cross = audit_legacy_candidate(cross_query, catalog)
                self.assertIs(cross.disposition, PromotionDisposition.FETCH_REQUIRED)
                self.assertEqual(
                    cross.reason_codes,
                    (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                )

                if source == "mlb_stats_pitcher_bf":
                    wrong_cutoff = dataclasses.replace(
                        exact_query,
                        parameters={
                            **exact_query.parameters,
                            "endDate": "2021-12-31",
                        },
                        requested_range={
                            **exact_query.requested_range,
                            "inclusive_end": "2021-12-31",
                        },
                    )
                    decision = audit_legacy_candidate(wrong_cutoff, catalog)
                    self.assertIs(
                        decision.disposition,
                        PromotionDisposition.FETCH_REQUIRED,
                    )
                    self.assertEqual(
                        decision.reason_codes,
                        (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                    )

    def test_protected_2021_career_bf_promotes_only_for_2022_plus_consumers(self):
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            repository = repository.resolve()
            legacy_root = repository / LEGACY_RELATIVE
            late = _career_query(inclusive_end="2021-12-31", cutoff="2022-01-01")
            legacy_query = {
                **late.parameters,
                "pitcher_id": 101,
                "url": f"{late.endpoint}?{urlencode(late.parameters)}",
            }
            RawStore(legacy_root).put(
                "mlb_stats_pitcher_bf", legacy_query, b'{"stats":[]}'
            )
            inventory_value = build_protected_raw_inventory(repository, legacy_root)
            inventory = repository / INVENTORY_RELATIVE
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes(_canonical_file(inventory_value))
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            catalog = _load(repository, legacy_root, inventory, digest)

            promoted = audit_legacy_candidate(late, catalog)
            self.assertIs(promoted.disposition, PromotionDisposition.PROMOTED)
            published = promote_legacy_candidate(
                _store(repository), late, catalog, promoted
            )
            self.assertEqual(published.storage_mode, "PROTECTED_LEGACY_REFERENCE")

            for query in (
                _career_query(inclusive_end="2021-12-31", cutoff="2021-12-31"),
                _career_query(inclusive_end="2017-12-31", cutoff="2021-12-31"),
            ):
                with self.subTest(query=query):
                    decision = audit_legacy_candidate(query, catalog)
                    self.assertIs(
                        decision.disposition, PromotionDisposition.FETCH_REQUIRED
                    )
                    self.assertEqual(
                        decision.reason_codes,
                        (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                    )

    def test_supported_nonstatcast_requested_ranges_are_closed(self) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
        )

        cases = (
            (
                "mlb_schedule",
                _legacy_schedule_query(),
                _schedule_raw(),
                _schedule_query(),
            ),
            (
                "mlb_stats_pitching",
                {
                    **_pitching_query().parameters,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(_pitching_query().parameters)
                    ),
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                _pitching_query(),
            ),
            (
                "mlb_stats_pitcher_bf",
                {
                    "endDate": "2017-12-31",
                    "gameType": "R",
                    "group": "pitching",
                    "pitcher_id": 101,
                    "sportIds": 1,
                    "startDate": "1876-01-01",
                    "stats": "byDateRange",
                    "url": (
                        "https://statsapi.mlb.com/api/v1/people/101/stats?"
                        + urlencode(_career_query().parameters)
                    ),
                },
                b'{"stats":[]}',
                _career_query(),
            ),
        )
        for source, legacy_query, raw, query in cases:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository = Path(directory) / "repository"
                repository.mkdir()
                repository = repository.resolve()
                legacy_root = repository / LEGACY_RELATIVE
                RawStore(legacy_root).put(source, legacy_query, raw)
                inventory_value = build_protected_raw_inventory(repository, legacy_root)
                inventory = repository / INVENTORY_RELATIVE
                inventory.parent.mkdir(parents=True)
                inventory.write_bytes(_canonical_file(inventory_value))
                digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
                catalog = _load(repository, legacy_root, inventory, digest)
                self.assertIs(
                    audit_legacy_candidate(query, catalog).disposition,
                    PromotionDisposition.PROMOTED,
                )
                changed = dataclasses.replace(
                    query,
                    requested_range={**query.requested_range, "unexpected": True},
                )
                decision = audit_legacy_candidate(changed, catalog)
                self.assertIs(decision.disposition, PromotionDisposition.FETCH_REQUIRED)
                self.assertEqual(
                    decision.reason_codes,
                    (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                )

    def test_nonstatcast_matchers_reject_every_noncanonical_current_identity(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            audit_legacy_candidate,
        )

        cases = (
            (
                "schedule",
                "mlb_schedule",
                _legacy_schedule_query(),
                _schedule_raw(),
                _schedule_query(),
                (
                    {"expected_media_type": "text/csv"},
                    {"parent_query_id": "c" * 64},
                    {"parser_version": "v5.2.0"},
                    {"source": "mlb_schedule"},
                    {"grain": "game_boxscore"},
                    {"endpoint": "https://statsapi.mlb.com/api/v1/game/9001/boxscore"},
                    {
                        "requested_range": {
                            **_schedule_query().requested_range,
                            "year": 2023,
                        }
                    },
                ),
            ),
            (
                "d1",
                "mlb_stats_pitching",
                {
                    **_pitching_query().parameters,
                    "url": (
                        "https://statsapi.mlb.com/api/v1/stats?"
                        + urlencode(_pitching_query().parameters)
                    ),
                },
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
                _pitching_query(),
                (
                    {"expected_media_type": "text/csv"},
                    {"parent_query_id": "c" * 64},
                    {"source": "mlb_stats_pitcher_bf"},
                    {"grain": "player_career_batters_faced_reference"},
                    {
                        "requested_range": {
                            **_pitching_query().requested_range,
                            "start_date": "2024-03-21",
                        }
                    },
                    {
                        "requested_range": {
                            **_pitching_query().requested_range,
                            "as_of_date": "2024-04-02",
                        }
                    },
                    {
                        "requested_range": {
                            **_pitching_query().requested_range,
                            "season": 2023,
                        }
                    },
                    {
                        "requested_range": {
                            **_pitching_query().requested_range,
                            "consumer_cutoff_dates": {"9001": "2024-03-31"},
                        }
                    },
                    {
                        "requested_range": {
                            **_pitching_query().requested_range,
                            "response_completeness": {
                                "limit": 4999,
                                "reject_total_splits_at_or_above_limit": True,
                            },
                        }
                    },
                ),
            ),
            (
                "career",
                "mlb_stats_pitcher_bf",
                {
                    "endDate": "2017-12-31",
                    "gameType": "R",
                    "group": "pitching",
                    "pitcher_id": 101,
                    "sportIds": 1,
                    "startDate": "1876-01-01",
                    "stats": "byDateRange",
                    "url": (
                        "https://statsapi.mlb.com/api/v1/people/101/stats?"
                        + urlencode(_career_query().parameters)
                    ),
                },
                b'{"stats":[]}',
                _career_query(),
                (
                    {"expected_media_type": "text/csv"},
                    {"parent_query_id": "c" * 64},
                    {"source": "mlb_stats_pitching"},
                    {"grain": "season_d1_league_pitching_reference"},
                    {"endpoint": "https://statsapi.mlb.com/api/v1/people/102/stats"},
                    {
                        "requested_range": {
                            **_career_query().requested_range,
                            "pitcher_id": 102,
                        }
                    },
                    {
                        "requested_range": {
                            **_career_query().requested_range,
                            "consumer_cutoff_dates": {"9001": "2017-12-30"},
                        }
                    },
                ),
            ),
        )
        for label, source, legacy_query, raw, query, mutations in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory) / "repository"
                repository.mkdir()
                repository = repository.resolve()
                legacy_root = repository / LEGACY_RELATIVE
                RawStore(legacy_root).put(source, legacy_query, raw)
                inventory_value = build_protected_raw_inventory(repository, legacy_root)
                inventory = repository / INVENTORY_RELATIVE
                inventory.parent.mkdir(parents=True)
                inventory.write_bytes(_canonical_file(inventory_value))
                digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
                catalog = _load(repository, legacy_root, inventory, digest)
                self.assertIs(
                    audit_legacy_candidate(query, catalog).disposition,
                    PromotionDisposition.PROMOTED,
                )
                for mutation in mutations:
                    with self.subTest(label=label, mutation=mutation):
                        decision = audit_legacy_candidate(
                            dataclasses.replace(query, **mutation), catalog
                        )
                        self.assertIs(
                            decision.disposition,
                            PromotionDisposition.FETCH_REQUIRED,
                        )
                        if "parser_version" in mutation:
                            expected_reason = (
                                PromotionReasonCode.CURRENT_PARSER_CONTRACT_UNSATISFIED
                            )
                        elif "source" in mutation and label != "schedule":
                            expected_reason = PromotionReasonCode.CURRENT_QUERY_INVALID
                        else:
                            expected_reason = (
                                PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE
                            )
                        self.assertEqual(
                            decision.reason_codes,
                            (expected_reason,),
                        )

        noncanonical_pairs = (
            (
                "d1-season-start",
                "mlb_stats_pitching",
                dataclasses.replace(
                    _pitching_query(),
                    parameters={
                        **_pitching_query().parameters,
                        "startDate": "2024-03-21",
                    },
                    requested_range={
                        **_pitching_query().requested_range,
                        "start_date": "2024-03-21",
                    },
                ),
                (SOURCE_FIXTURES / "pitching_mini.json").read_bytes(),
            ),
            (
                "career-start",
                "mlb_stats_pitcher_bf",
                dataclasses.replace(
                    _career_query(),
                    parameters={
                        **_career_query().parameters,
                        "startDate": "1900-01-01",
                    },
                ),
                b'{"stats":[]}',
            ),
            (
                "career-end",
                "mlb_stats_pitcher_bf",
                dataclasses.replace(
                    _career_query(),
                    parameters={
                        **_career_query().parameters,
                        "endDate": "2021-12-31",
                    },
                    requested_range={
                        **_career_query().requested_range,
                        "inclusive_end": "2021-12-31",
                    },
                ),
                b'{"stats":[]}',
            ),
        )
        for label, source, query, raw in noncanonical_pairs:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory) / "repository"
                repository.mkdir()
                repository = repository.resolve()
                legacy_root = repository / LEGACY_RELATIVE
                legacy_query = dict(query.parameters)
                if source == "mlb_stats_pitcher_bf":
                    legacy_query["pitcher_id"] = query.requested_range["pitcher_id"]
                legacy_query["url"] = f"{query.endpoint}?{urlencode(query.parameters)}"
                RawStore(legacy_root).put(source, legacy_query, raw)
                inventory_value = build_protected_raw_inventory(repository, legacy_root)
                inventory = repository / INVENTORY_RELATIVE
                inventory.parent.mkdir(parents=True)
                inventory.write_bytes(_canonical_file(inventory_value))
                digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
                decision = audit_legacy_candidate(
                    query, _load(repository, legacy_root, inventory, digest)
                )
                self.assertIs(decision.disposition, PromotionDisposition.FETCH_REQUIRED)
                self.assertEqual(
                    decision.reason_codes,
                    (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                )

    def test_alias_ambiguity_is_fatal(self) -> None:
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            with patch.object(
                type(catalog),
                "records",
                new_callable=PropertyMock,
                return_value=catalog.records * 2,
            ):
                decision = audit_legacy_candidate(_schedule_query(), catalog)
            self.assertIs(decision.disposition, PromotionDisposition.FATAL_INTEGRITY)
            self.assertIsNone(decision.candidate)

    def test_audit_rejects_same_bytes_inventory_or_payload_inode_replacement(self):
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            audit_legacy_candidate,
        )

        for target_name in ("inventory", "payload"):
            with (
                self.subTest(target=target_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository, legacy_root, inventory, digest = _repository(
                    Path(directory)
                )
                catalog = _load(repository, legacy_root, inventory, digest)
                target = (
                    inventory
                    if target_name == "inventory"
                    else catalog.catalog.candidates[0].payload_path
                )
                contents = target.read_bytes()
                original_inode = target.stat().st_ino
                replacement = target.with_suffix(target.suffix + ".replacement")
                replacement.write_bytes(contents)
                os.replace(replacement, target)
                self.assertNotEqual(target.stat().st_ino, original_inode)
                decision = audit_legacy_candidate(_schedule_query(), catalog)
                self.assertIs(
                    decision.disposition, PromotionDisposition.FATAL_INTEGRITY
                )

    def test_catalog_snapshots_open_only_root_absolute_then_dirfd_components(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            original_open = os.open
            forbidden_absolute_opens: list[Path] = []
            leaf_without_nonblock: list[str] = []

            def recording_open(path, flags, *args, **kwargs):
                candidate = Path(os.fspath(path))
                if candidate.is_absolute() and candidate != Path("/"):
                    forbidden_absolute_opens.append(candidate)
                if (
                    kwargs.get("dir_fd") is not None
                    and not flags & getattr(os, "O_DIRECTORY", 0)
                    and not flags & getattr(os, "O_NONBLOCK", 0)
                ):
                    leaf_without_nonblock.append(os.fspath(path))
                return original_open(path, flags, *args, **kwargs)

            inventory_value = json.loads(inventory.read_bytes())
            with (
                patch.object(
                    legacy_module,
                    "verify_protected_raw_inventory",
                    return_value=inventory_value,
                ),
                patch.object(legacy_module.os, "open", side_effect=recording_open),
            ):
                _load(repository, legacy_root, inventory, digest)
            self.assertEqual(forbidden_absolute_opens, [])
            self.assertEqual(leaf_without_nonblock, [])

    def test_catalog_rejects_ancestor_swap_during_initial_descriptor_snapshot(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            audit = repository / "audit"
            replacement = repository / "audit.replacement"
            saved = repository / "audit.saved"
            shutil.copytree(audit, replacement, copy_function=shutil.copy2)
            original_open = os.open
            swapped = False

            def swapping_open(path, flags, *args, **kwargs):
                nonlocal swapped
                value = os.fspath(path)
                if not swapped and (value == "audit" or Path(value) == inventory):
                    swapped = True
                    audit.rename(saved)
                    replacement.rename(audit)
                return original_open(path, flags, *args, **kwargs)

            with (
                patch.object(legacy_module.os, "open", side_effect=swapping_open),
                self.assertRaises(ProtectedLegacyIntegrityError),
            ):
                _load(repository, legacy_root, inventory, digest)
            self.assertTrue(swapped)

    def test_catalog_rejects_ancestor_symlink_swap_during_component_open_without_fd_leak(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            audit = repository / "audit"
            saved = repository / "audit.saved"
            original_open = os.open
            before_fds = set(os.listdir("/dev/fd"))
            swapped = False

            def swapping_open(path, flags, *args, **kwargs):
                nonlocal swapped
                value = os.fspath(path)
                if not swapped and value == "audit":
                    swapped = True
                    audit.rename(saved)
                    audit.symlink_to(saved.name, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)

            with (
                patch.object(legacy_module.os, "open", side_effect=swapping_open),
                self.assertRaises(ProtectedLegacyIntegrityError) as caught,
            ):
                _load(repository, legacy_root, inventory, digest)
            self.assertTrue(swapped)
            self.assertIn(
                caught.exception.reason_code,
                {
                    legacy_module.PromotionReasonCode.LEGACY_LINK_TYPE_INVALID,
                    legacy_module.PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED,
                },
            )
            self.assertEqual(set(os.listdir("/dev/fd")), before_fds)

    def test_catalog_collective_rebind_rejects_same_leaf_inodes_under_swapped_ancestor(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            initial = _load(repository, legacy_root, inventory, digest)
            payload = initial.catalog.candidates[0].payload_path
            payload_inode = payload.stat().st_ino
            source = payload.parent
            saved = source.with_name(source.name + ".saved")
            original_read = os.read
            original_verify = legacy_module.verify_protected_raw_inventory
            swapped = False
            verified = False

            def mark_verified(*args, **kwargs):
                nonlocal verified
                result = original_verify(*args, **kwargs)
                verified = True
                return result

            def swapping_read(descriptor, size):
                nonlocal swapped
                contents = original_read(descriptor, size)
                if (
                    not swapped
                    and verified
                    and not contents
                    and os.fstat(descriptor).st_ino == payload_inode
                ):
                    swapped = True
                    source.rename(saved)
                    source.mkdir()
                    for leaf in saved.iterdir():
                        leaf.rename(source / leaf.name)
                    saved.rmdir()
                    self.assertEqual(payload.stat().st_ino, payload_inode)
                return contents

            with (
                patch.object(
                    legacy_module,
                    "verify_protected_raw_inventory",
                    side_effect=mark_verified,
                ),
                patch.object(legacy_module.os, "read", side_effect=swapping_read),
            ):
                catalog = _load(repository, legacy_root, inventory, digest)
            self.assertFalse(swapped)
            self.assertEqual(len(catalog.catalog.candidates), 1)

    def test_catalog_final_collective_rebind_rechecks_earlier_record_after_later_read(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            repository = repository.resolve()
            legacy_root = repository / LEGACY_RELATIVE
            RawStore(legacy_root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes(),
            )
            RawStore(legacy_root).put(
                "mlb_schedule", _legacy_schedule_query(), _schedule_raw()
            )
            inventory_value = build_protected_raw_inventory(repository, legacy_root)
            inventory = repository / INVENTORY_RELATIVE
            inventory.parent.mkdir(parents=True)
            inventory.write_bytes(_canonical_file(inventory_value))
            digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
            catalog = _load(repository, legacy_root, inventory, digest)
            first, second = catalog.catalog.candidates
            first_source = first.payload_path.parent
            saved = first_source.with_name(first_source.name + ".saved")
            second_inode = second.payload_path.stat().st_ino
            original_read = os.read
            original_verify = legacy_module.verify_protected_raw_inventory
            swapped = False
            verified = False

            def mark_verified(*args, **kwargs):
                nonlocal verified
                result = original_verify(*args, **kwargs)
                verified = True
                return result

            def swapping_read(descriptor, size):
                nonlocal swapped
                contents = original_read(descriptor, size)
                if (
                    not swapped
                    and verified
                    and not contents
                    and os.fstat(descriptor).st_ino == second_inode
                ):
                    swapped = True
                    first_source.rename(saved)
                    first_source.mkdir()
                    for leaf in saved.iterdir():
                        leaf.rename(first_source / leaf.name)
                    saved.rmdir()
                return contents

            with (
                patch.object(
                    legacy_module,
                    "verify_protected_raw_inventory",
                    side_effect=mark_verified,
                ),
                patch.object(legacy_module.os, "read", side_effect=swapping_read),
            ):
                catalog = _load(repository, legacy_root, inventory, digest)
            self.assertFalse(swapped)
            self.assertEqual(len(catalog.catalog.candidates), 2)

    def test_catalog_rejects_same_bytes_member_replacement_after_inventory_verifier(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module
        from mlb_backtest.v52_legacy import ProtectedLegacyIntegrityError

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            inventory_value = json.loads(inventory.read_bytes())
            payload = repository / inventory_value["records"][0]["raw"]["path"]
            original_verify = legacy_module.verify_protected_raw_inventory
            replaced = False

            def verify_then_replace(*args, **kwargs):
                nonlocal replaced
                result = original_verify(*args, **kwargs)
                if not replaced:
                    replaced = True
                    replacement = payload.with_suffix(payload.suffix + ".replacement")
                    replacement.write_bytes(payload.read_bytes())
                    os.replace(replacement, payload)
                return result

            with (
                patch.object(
                    legacy_module,
                    "verify_protected_raw_inventory",
                    side_effect=verify_then_replace,
                ),
                self.assertRaises(ProtectedLegacyIntegrityError) as caught,
            ):
                _load(repository, legacy_root, inventory, digest)
            self.assertTrue(replaced)
            self.assertEqual(
                caught.exception.reason_code,
                legacy_module.PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED,
            )

    def test_promotion_reaudits_and_publishes_reference_without_raw_or_attempt(self):
        from mlb_backtest.v52_legacy import (
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            query = _schedule_query()
            decision = audit_legacy_candidate(query, catalog)
            store = _store(repository)
            published = promote_legacy_candidate(store, query, catalog, decision)
            self.assertEqual(published.storage_mode, "PROTECTED_LEGACY_REFERENCE")
            verified = store.read_verified_success(query)
            self.assertEqual(verified.published_evidence, published)
            self.assertEqual(verified.attempt_receipts, ())
            folder = store.root / "queries" / query.source / logical_query_id(query)
            self.assertEqual(list((folder / "attempts").iterdir()), [])
            self.assertFalse(any((folder / "objects").glob("*.raw")))
            reference = json.loads(
                next((folder / "objects").glob("*.json")).read_bytes()
            )
            self.assertEqual(reference["source"], query.source)
            self.assertEqual(reference["legacy_source"], "mlb_schedule")
            protected_inode = catalog.catalog.candidates[0].payload_path.stat().st_ino
            for leaf in folder.rglob("*"):
                if leaf.is_file():
                    self.assertEqual(leaf.stat().st_nlink, 1)
                    self.assertNotEqual(leaf.stat().st_ino, protected_inode)

    def test_public_dispositions_have_closed_deterministic_reason_codes(self) -> None:
        import mlb_backtest.v52_legacy as legacy_module
        from mlb_backtest.v52_legacy import (
            PromotionDisposition,
            PromotionReasonCode,
            ProtectedLegacyIntegrityError,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            promoted = audit_legacy_candidate(_schedule_query(), catalog)
            no_match = audit_legacy_candidate(_career_query(), catalog)
            malformed = audit_legacy_candidate(
                dataclasses.replace(_schedule_query(), parent_query_id="C" * 64),
                catalog,
            )

            class TypeErrorMapping(dict):
                def __iter__(self):
                    raise TypeError("malformed mapping iteration")

            malformed_mapping = audit_legacy_candidate(
                dataclasses.replace(
                    _schedule_query(),
                    parameters=TypeErrorMapping(_schedule_query().parameters),
                ),
                catalog,
            )
            with patch.object(
                type(catalog),
                "records",
                new_callable=PropertyMock,
                return_value=catalog.records * 2,
            ):
                ambiguous = audit_legacy_candidate(_schedule_query(), catalog)
            self.assertEqual(
                (
                    promoted.disposition,
                    promoted.reason_codes,
                    no_match.disposition,
                    no_match.reason_codes,
                    malformed.disposition,
                    malformed.reason_codes,
                    ambiguous.disposition,
                    ambiguous.reason_codes,
                    malformed_mapping.disposition,
                    malformed_mapping.reason_codes,
                ),
                (
                    PromotionDisposition.PROMOTED,
                    (),
                    PromotionDisposition.FETCH_REQUIRED,
                    (PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,),
                    PromotionDisposition.FETCH_REQUIRED,
                    (PromotionReasonCode.CURRENT_QUERY_INVALID,),
                    PromotionDisposition.FATAL_INTEGRITY,
                    (PromotionReasonCode.CATALOG_NOT_UNIQUE,),
                    PromotionDisposition.FETCH_REQUIRED,
                    (PromotionReasonCode.CURRENT_QUERY_INVALID,),
                ),
            )

            propagated = tuple(
                reason
                for reason in PromotionReasonCode
                if reason
                not in {
                    PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,
                    PromotionReasonCode.CURRENT_PARSER_CONTRACT_UNSATISFIED,
                    PromotionReasonCode.CURRENT_SEMANTIC_CONTRACT_UNSATISFIED,
                    PromotionReasonCode.CURRENT_QUERY_INVALID,
                    PromotionReasonCode.PROTECTED_PUBLISH_REVERIFY_FAILED,
                }
            )
            for reason in propagated:
                with (
                    self.subTest(reason=reason),
                    patch.object(
                        legacy_module,
                        "_revalidate_catalog",
                        side_effect=ProtectedLegacyIntegrityError(reason),
                    ),
                ):
                    decision = audit_legacy_candidate(_schedule_query(), catalog)
                    self.assertIs(
                        decision.disposition, PromotionDisposition.FATAL_INTEGRITY
                    )
                    self.assertEqual(decision.reason_codes, (reason,))

            original_verify = legacy_module.verify_protected_raw_inventory
            payload = catalog.catalog.candidates[0].payload_path
            replaced = False

            def verify_then_replace(*args, **kwargs):
                nonlocal replaced
                result = original_verify(*args, **kwargs)
                if not replaced:
                    replaced = True
                    replacement = payload.with_suffix(payload.suffix + ".replacement")
                    replacement.write_bytes(payload.read_bytes())
                    os.replace(replacement, payload)
                return result

            with patch.object(
                legacy_module,
                "verify_protected_raw_inventory",
                side_effect=verify_then_replace,
            ):
                member_reverify = audit_legacy_candidate(_schedule_query(), catalog)
            self.assertFalse(replaced)
            self.assertEqual(
                member_reverify.disposition,
                PromotionDisposition.PROMOTED,
            )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(
                Path(directory), raw=b"not-current-schedule-json\n"
            )
            semantic = audit_legacy_candidate(
                _schedule_query(), _load(repository, legacy_root, inventory, digest)
            )
            self.assertEqual(
                semantic.reason_codes,
                (PromotionReasonCode.CURRENT_SEMANTIC_CONTRACT_UNSATISFIED,),
            )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            query = _schedule_query()
            decision = audit_legacy_candidate(query, catalog)
            payload = catalog.catalog.candidates[0].payload_path
            payload.write_bytes(payload.read_bytes() + b"changed")
            with self.assertRaises(ProtectedLegacyIntegrityError) as caught:
                promote_legacy_candidate(_store(repository), query, catalog, decision)
            self.assertEqual(
                caught.exception.reason_code,
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED,
            )

    def test_promotion_fails_fatal_when_payload_changes_after_audit(self) -> None:
        from mlb_backtest.v52_legacy import (
            ProtectedLegacyIntegrityError,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            query = _schedule_query()
            decision = audit_legacy_candidate(query, catalog)
            payload = repository / catalog.catalog.candidates[0].payload_path
            payload.write_bytes(payload.read_bytes() + b"changed")
            store = _store(repository)
            with self.assertRaises(ProtectedLegacyIntegrityError):
                promote_legacy_candidate(store, query, catalog, decision)
            success = (
                store.root
                / "queries"
                / query.source
                / logical_query_id(query)
                / "success.json"
            )
            self.assertFalse(success.exists())

    def test_promotion_rejects_payload_mutation_after_semantic_reaudit(self) -> None:
        import mlb_backtest.v52_legacy as legacy_module
        from mlb_backtest.v52_legacy import (
            ProtectedLegacyIntegrityError,
            audit_legacy_candidate,
            promote_legacy_candidate,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository, legacy_root, inventory, digest = _repository(Path(directory))
            catalog = _load(repository, legacy_root, inventory, digest)
            query = _schedule_query()
            decision = audit_legacy_candidate(query, catalog)
            payload = catalog.catalog.candidates[0].payload_path
            original_recompute = legacy_module.recompute_query_sidecar
            calls = 0

            def recompute_then_replace(*args, **kwargs):
                nonlocal calls
                result = original_recompute(*args, **kwargs)
                calls += 1
                if calls == 1:
                    contents = payload.read_bytes()
                    replacement = payload.with_suffix(".replacement")
                    replacement.write_bytes(contents)
                    os.replace(replacement, payload)
                return result

            store = _store(repository)
            with (
                patch.object(
                    legacy_module,
                    "recompute_query_sidecar",
                    side_effect=recompute_then_replace,
                ),
                patch.object(
                    store,
                    "publish_fetched",
                    side_effect=AssertionError("network/fetch path forbidden"),
                ),
                self.assertRaises(ProtectedLegacyIntegrityError),
            ):
                promote_legacy_candidate(store, query, catalog, decision)


class LogicalProtectedRawRecordTests(unittest.TestCase):
    def test_r4_logical_record_never_serializes_device_identity(self) -> None:
        from mlb_backtest.v52_legacy import LogicalProtectedRawRecord

        record = LogicalProtectedRawRecord(
            PurePosixPath("mlb_schedule/a.raw"), "a" * 64, 7
        )
        self.assertEqual(
            record.as_mapping(),
            {"path": "mlb_schedule/a.raw", "sha256": "a" * 64, "byte_size": 7},
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(record)},
            {"path", "sha256", "byte_size"},
        )

    def test_logical_record_rejects_noncanonical_paths(self) -> None:
        from mlb_backtest.v52_legacy import LogicalProtectedRawRecord

        for path in (
            PurePosixPath("/mlb_schedule/a.raw"),
            PurePosixPath("."),
            PurePosixPath("mlb_schedule/a\\x.raw"),
        ):
            with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                LogicalProtectedRawRecord(path, "a" * 64, 0)

    def test_logical_record_rejects_bad_sha_and_size(self) -> None:
        from mlb_backtest.v52_legacy import LogicalProtectedRawRecord

        for sha in ("A" * 64, "a" * 63, "x" * 64, None):
            with self.subTest(sha=sha), self.assertRaises((TypeError, ValueError)):
                LogicalProtectedRawRecord(PurePosixPath("a.raw"), sha, 0)  # type: ignore[arg-type]
        for size in (-1, True, 1.0, None):
            with self.subTest(size=size), self.assertRaises((TypeError, ValueError)):
                LogicalProtectedRawRecord(PurePosixPath("a.raw"), "a" * 64, size)  # type: ignore[arg-type]

    def test_existing_r3_inventory_still_requires_device_and_inode(self) -> None:
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        inventory = json.loads((ROOT / INVENTORY_RELATIVE).read_text())
        del inventory["records"][0]["manifest"]["inode_fingerprint"]
        with self.assertRaises(ValueError):
            parse_protected_raw_inventory_document(_canonical_file(inventory))


if __name__ == "__main__":
    unittest.main()
