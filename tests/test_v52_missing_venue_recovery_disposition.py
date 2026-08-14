from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.v52_evidence import logical_query_id


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PARENT_ROOT = Path(os.environ["MLB_V52_SOURCE_AUTHORITY_ROOT"]).resolve()
R3_RUN = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
)
R3_CONTRACT = Path(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
)
SHARED_ENDPOINT = "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924"


class MissingVenueRecoveryDispositionTests(unittest.TestCase):
    """r3 disposition construction has no injectable synthetic authority seam."""

    def _catalog(self) -> object:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            build_missing_venue_recovery_disposition_catalog,
        )

        with patch.dict(
            os.environ,
            {"MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT": str(PROTECTED_PARENT_ROOT)},
            clear=False,
        ):
            return build_missing_venue_recovery_disposition_catalog(
                ROOT, ROOT / R3_RUN, ROOT / R3_CONTRACT
            )

    def test_r3_catalog_does_not_call_r2_private_reconciliation(self) -> None:
        from mlb_backtest import v52_schedule_conflict_recovery_disposition as r2

        with patch.object(
            r2,
            "build_recovery_schedule_conflict_catalog",
            side_effect=AssertionError("r2 reconciliation is forbidden"),
        ):
            self.assertEqual(
                self._catalog().game_pks(), (565534, 630882, 633224, 632924)
            )

    def test_factory_does_not_load_a_poisoned_r2_reconciliation_module(self) -> None:
        from mlb_backtest import v52_schedule_conflict_recovery_disposition as r2

        with patch.object(
            r2,
            "_recompute",
            side_effect=AssertionError("r2 reconciliation is forbidden"),
        ):
            self.assertEqual(
                self._catalog().game_pks(), (565534, 630882, 633224, 632924)
            )

    def test_public_factory_builds_the_exact_four_record_projection(self) -> None:
        catalog = self._catalog()

        self.assertEqual(catalog.game_pks(), (565534, 630882, 633224, 632924))
        self.assertEqual(catalog.record_to_projection_indexes(), (0, 1, 2, 2))
        self.assertEqual(catalog.excluded_game_pks(), frozenset({632924}))
        self.assertEqual(len(catalog.projection_query_ids()), 3)
        self.assertEqual(
            tuple(logical_query_id(record.query) for record in catalog.records),
            (
                *catalog.projection_query_ids(),
                catalog.projection_query_ids()[2],
            ),
        )
        self.assertEqual(
            catalog.projection_query_for_game_pk(633224).endpoint, SHARED_ENDPOINT
        )
        self.assertEqual(
            catalog.projection_query_for_game_pk(632924).endpoint, SHARED_ENDPOINT
        )
        self.assertEqual(
            tuple(
                record.game_pk
                for record in catalog.records_for_query(
                    catalog.projection_query_for_game_pk(632924)
                )
            ),
            (633224, 632924),
        )

    def test_record_semantics_sha256_reads_only_sealed_canonical_bytes(self) -> None:
        record = self._catalog().records[0]

        digest = record.semantics_sha256
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            hashlib.sha256(record._semantics_canonical_bytes).hexdigest(),
        )
        self.assertEqual(digest, record.semantics_sha256)

        object.__setattr__(record, "_semantics_canonical_bytes", b"{}")
        with self.assertRaisesRegex(ValueError, "r3 disposition semantics"):
            _ = record.semantics_sha256

    def test_projection_adapter_is_shared_immutable_and_fails_closed(self) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionProjection,
        )

        projection = self._catalog().as_projection()
        self.assertIs(type(projection), RecoveryDispositionProjection)
        self.assertEqual(
            projection.record_game_pks, (565534, 630882, 633224, 632924)
        )
        self.assertEqual(projection.record_to_projection_indexes, (0, 1, 2, 2))
        self.assertEqual(projection.excluded_game_pks, frozenset({632924}))
        self.assertEqual(
            projection.projection_query_id_for_game_pk(633224),
            projection.projection_query_id_for_game_pk(632924),
        )
        with self.assertRaises(FrozenInstanceError):
            projection.record_game_pks = ()

        valid_ids = ("a" * 64, "b" * 64, "c" * 64)
        invalid_cases = (
            ((565534,), valid_ids, (0, 1), frozenset()),
            ((565534, 630882), ("a" * 64, "a" * 64), (0, 1), frozenset()),
            ((565534, 630882), valid_ids[:2], (0, 2), frozenset()),
            ((565534, 630882), valid_ids, (0, 1), frozenset()),
            ((630882, 565534), valid_ids[:2], (0, 1), frozenset()),
            ((565534, 630882), valid_ids[:2], (0, 1), frozenset({632924})),
        )
        for game_pks, query_ids, indexes, excluded in invalid_cases:
            with self.subTest(game_pks=game_pks, indexes=indexes):
                with self.assertRaises(ValueError):
                    RecoveryDispositionProjection(
                        game_pks, query_ids, indexes, excluded
                    )

    def test_factory_recomputes_all_three_conflicts_from_authenticated_parents(
        self,
    ) -> None:
        from mlb_backtest import v52_missing_venue_recovery_disposition as disposition
        from mlb_backtest.v52_recovery_parent_evidence import RecoveryParentCatalog

        with (
            patch.object(
                RecoveryParentCatalog,
                "read_raw",
                wraps=RecoveryParentCatalog.read_raw,
                autospec=True,
            ) as read_raw,
            patch.object(
                RecoveryParentCatalog,
                "read_json",
                wraps=RecoveryParentCatalog.read_json,
                autospec=True,
            ) as read_json,
            patch.object(
                RecoveryParentCatalog,
                "reauthenticate",
                wraps=RecoveryParentCatalog.reauthenticate,
                autospec=True,
            ) as reauthenticate,
        ):
            catalog = self._catalog()

        self.assertTrue(hasattr(disposition, "_build_r3_conflict_records"))
        self.assertEqual(read_json.call_count, 0)
        self.assertLessEqual(read_raw.call_count, 4)
        self.assertLessEqual(reauthenticate.call_count, 9)
        self.assertEqual(
            tuple(call.args[1] for call in read_raw.call_args_list),
            (
                "original_2019_565534",
                "superseding_2020_630882",
                "superseding_2021_633224",
            ),
        )
        for parent_name in (
            "original_2019_565534",
            "superseding_2020_630882",
            "superseding_2021_633224",
        ):
            self.assertIn(
                parent_name, tuple(call.args[1] for call in read_raw.call_args_list)
            )
        expected = {
            565534: ((54, 5), (161, 12), ("5d2fc494", "6b07d5e4")),
            630882: ((17, 0), (22, 0), ("2a5b2517", "0d961f12")),
            633224: (
                ((106, 12), (108, 2)),
                (173, 11),
                ("b5d52997", "25f8ab07", "109a5d14"),
            ),
        }
        for game_pk, (suppressed, retained, digests) in expected.items():
            with self.subTest(game_pk=game_pk):
                semantics = catalog.for_game_pk(game_pk).semantics
                self.assertEqual(semantics["parent_recomputed"], True)
                actual_suppressed = semantics.get(
                    "suppressed_occurrences", (semantics.get("suppressed_occurrence"),)
                )
                self.assertEqual(
                    tuple(
                        (item["date_index"], item["game_index"])
                        for item in actual_suppressed
                    ),
                    suppressed if isinstance(suppressed[0], tuple) else (suppressed,),
                )
                self.assertEqual(
                    (
                        semantics["retained_occurrence"]["date_index"],
                        semantics["retained_occurrence"]["game_index"],
                    ),
                    retained,
                )
                self.assertEqual(
                    tuple(item["sha256"][:8] for item in actual_suppressed)
                    + (semantics["retained_occurrence"]["sha256"][:8],),
                    digests,
                )

    def test_static_batch_validator_rejects_every_noncanonical_batch_shape(
        self,
    ) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            _validate_r3_static_batch,
        )
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(ROOT / R3_RUN, ROOT / R3_CONTRACT)
        run_sha = hashlib.sha256((ROOT / R3_RUN).read_bytes()).hexdigest()
        contract_sha = hashlib.sha256((ROOT / R3_CONTRACT).read_bytes()).hexdigest()
        _validate_r3_static_batch(batch, run_sha, contract_sha)
        cases = {
            "batch_id": dataclasses.replace(batch, batch_id="0" * 64),
            "stage": dataclasses.replace(batch, stage="REFERENCE_INPUTS"),
            "derived": dataclasses.replace(batch, derived_from_sha256="0" * 64),
            "count": dataclasses.replace(batch, queries=batch.queries[:-1]),
            "order": dataclasses.replace(
                batch, queries=(batch.queries[1], batch.queries[0], *batch.queries[2:])
            ),
            "duplicate": dataclasses.replace(
                batch, queries=(batch.queries[0], batch.queries[0], *batch.queries[2:])
            ),
            "standalone": dataclasses.replace(
                batch,
                queries=(
                    *batch.queries[:3],
                    dataclasses.replace(
                        batch.queries[3],
                        endpoint="disposition://MISSING_VENUE_632924",
                    ),
                    *batch.queries[4:],
                ),
            ),
        }
        for label, forged in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    _validate_r3_static_batch(forged, run_sha, contract_sha)

    def test_static_batch_validator_rejects_every_query_identity_field(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            _validate_r3_static_batch,
        )
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(ROOT / R3_RUN, ROOT / R3_CONTRACT)
        run_sha = hashlib.sha256((ROOT / R3_RUN).read_bytes()).hexdigest()
        contract_sha = hashlib.sha256((ROOT / R3_CONTRACT).read_bytes()).hexdigest()
        changes = (
            ("source", {"source": "forged"}),
            ("grain", {"grain": "forged"}),
            ("endpoint", {"endpoint": "disposition://forged"}),
            ("parameters", {"parameters": {"forged": True}}),
            (
                "requested_range",
                {
                    "requested_range": {
                        "year": 1900,
                        "start_date": "1900-01-01",
                        "end_date": "1900-12-31",
                    }
                },
            ),
            ("expected_media_type", {"expected_media_type": "text/plain"}),
            ("parser_version", {"parser_version": "v0"}),
            ("parent_query_id", {"parent_query_id": "0" * 64}),
            ("run_spec_sha256", {"run_spec_sha256": "0" * 64}),
            ("acquisition_contract_sha256", {"acquisition_contract_sha256": "0" * 64}),
        )
        for index in range(7):
            for field, changeset in changes:
                with self.subTest(index=index, field=field):
                    queries = list(batch.queries)
                    queries[index] = dataclasses.replace(queries[index], **changeset)
                    forged = dataclasses.replace(batch, queries=tuple(queries))
                    with self.assertRaises(ValueError):
                        _validate_r3_static_batch(forged, run_sha, contract_sha)

    def test_static_query_rejects_non_mapping_and_other_malformed_fields(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            _validate_static_query,
        )
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(ROOT / R3_RUN, ROOT / R3_CONTRACT)
        run_sha = hashlib.sha256((ROOT / R3_RUN).read_bytes()).hexdigest()
        contract_sha = hashlib.sha256((ROOT / R3_CONTRACT).read_bytes()).hexdigest()
        malformed = (
            (1, {"parameters": []}),
            (0, {"requested_range": list(batch.queries[0].requested_range.items())}),
            (0, {"source": None}),
            (0, {"expected_media_type": None}),
            (0, {"parent_query_id": 1}),
            (0, {"run_spec_sha256": None}),
        )
        for index, changeset in malformed:
            with self.subTest(index=index, changeset=changeset):
                with self.assertRaises(ValueError):
                    _validate_static_query(
                        dataclasses.replace(batch.queries[index], **changeset),
                        index,
                        run_sha,
                        contract_sha,
                    )

    def test_r3_conflict_authority_rejects_parent_metadata_and_position_mutations(
        self,
    ) -> None:
        from mlb_backtest import v52_missing_venue_recovery_disposition as disposition
        from mlb_backtest.v52_query_plan import build_static_batch
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )

        parents = build_recovery_parent_catalog(PROTECTED_PARENT_ROOT)
        batch = build_static_batch(ROOT / R3_RUN, ROOT / R3_CONTRACT)
        projections = tuple(
            query
            for query in batch.queries
            if query.endpoint.startswith("disposition://")
        )
        original = parents.parents["superseding_2020_630882"]
        for artifact in ("registration", "attempt", "quarantine", "raw"):
            for field, value in (
                ("relative", Path("data/forged")),
                ("sha256", "0" * 64),
                ("byte_count", 0),
            ):
                with self.subTest(artifact=artifact, field=field):
                    changed_file = dataclasses.replace(
                        getattr(original, artifact), **{field: value}
                    )
                    changed_parent = dataclasses.replace(
                        original, **{artifact: changed_file}
                    )
                    forged = dataclasses.replace(
                        parents,
                        parents={
                            **parents.parents,
                            "superseding_2020_630882": changed_parent,
                        },
                    )
                    with self.assertRaises(ValueError):
                        disposition._build_r3_conflict_records(forged, projections)
        mutated_specs = list(disposition._R3_CONFLICT_AUTHORITY)
        mutated_specs[0] = dataclasses.replace(
            mutated_specs[0], suppressed_positions=((0, 0),)
        )
        with patch.object(disposition, "_R3_CONFLICT_AUTHORITY", tuple(mutated_specs)):
            with self.assertRaises(ValueError):
                disposition._build_r3_conflict_records(parents, projections)

    def test_missing_venue_binding_rejects_each_parent_file_identity_mutation(
        self,
    ) -> None:
        from mlb_backtest import v52_missing_venue_recovery_disposition as disposition
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )

        catalog = build_recovery_parent_catalog(PROTECTED_PARENT_ROOT)
        original = catalog.parents["superseding_2021_633224"]
        for artifact in ("registration", "attempt", "quarantine", "raw"):
            for field, replacement in (
                ("relative", Path("data/forged")),
                ("sha256", "0" * 64),
                ("byte_count", 0),
            ):
                with self.subTest(artifact=artifact, field=field):
                    changed = dataclasses.replace(
                        getattr(original, artifact), **{field: replacement}
                    )
                    forged = dataclasses.replace(
                        catalog,
                        parents={
                            **catalog.parents,
                            "superseding_2021_633224": dataclasses.replace(
                                original, **{artifact: changed}
                            ),
                        },
                    )
                    with self.assertRaisesRegex(ValueError, "parent binding differs"):
                        disposition._missing_venue_parent_binding(forged)

    def test_missing_venue_raw_guard_rejects_position_and_every_fixed_fact_mutation(
        self,
    ) -> None:
        from mlb_backtest import v52_missing_venue_recovery_disposition as disposition
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )

        catalog = build_recovery_parent_catalog(PROTECTED_PARENT_ROOT)
        raw = catalog.read_raw("superseding_2021_633224")
        mutations = {
            "canonical_no_lf_digest": lambda game: game.__setitem__(
                "description", "forged"
            ),
            "game_pk": lambda game: game.__setitem__("gamePk", 1),
            "official_date": lambda game: game.__setitem__(
                "officialDate", "1900-01-01"
            ),
            "game_date": lambda game: game.__setitem__(
                "gameDate", "1900-01-01T00:00:00Z"
            ),
            "calendar_event": lambda game: game.__setitem__(
                "calendarEventID", "forged"
            ),
            "away_team_id": lambda game: game["teams"]["away"]["team"].__setitem__(
                "id", 1
            ),
            "away_team_name": lambda game: game["teams"]["away"]["team"].__setitem__(
                "name", "forged"
            ),
            "away_score": lambda game: game["teams"]["away"].__setitem__("score", 1),
            "away_winner": lambda game: game["teams"]["away"].__setitem__(
                "isWinner", True
            ),
            "home_team_id": lambda game: game["teams"]["home"]["team"].__setitem__(
                "id", 1
            ),
            "home_team_name": lambda game: game["teams"]["home"]["team"].__setitem__(
                "name", "forged"
            ),
            "home_score": lambda game: game["teams"]["home"].__setitem__("score", 1),
            "home_winner": lambda game: game["teams"]["home"].__setitem__(
                "isWinner", False
            ),
            "terminal_status": lambda game: game["status"].__setitem__(
                "detailedState", "Scheduled"
            ),
            "venue_link": lambda game: game["venue"].__setitem__(
                "link", "/api/v1/venues/1"
            ),
            "venue_id": lambda game: game["venue"].__setitem__("id", 1),
            "venue_name": lambda game: game["venue"].__setitem__("name", "forged"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = json.loads(raw)
                game = payload["dates"][130]["games"][10]
                mutate(game)  # type: ignore[operator]
                with self.assertRaisesRegex(
                    ValueError, "missing-venue raw object differs"
                ):
                    disposition._missing_venue_raw_object(
                        json.dumps(
                            payload, separators=(",", ":"), sort_keys=True
                        ).encode()
                    )
        with patch.object(disposition, "_MISSING_VENUE_POSITION", (0, 0)):
            with self.assertRaisesRegex(ValueError, "missing-venue raw object differs"):
                disposition._missing_venue_raw_object(raw)
        real_sha256 = hashlib.sha256

        def lf_digest(data: bytes) -> object:
            if data.endswith(b"\n"):
                return types.SimpleNamespace(
                    hexdigest=lambda: disposition._MISSING_VENUE_OBJECT_SHA256
                )
            return real_sha256(data)

        with patch.object(
            disposition, "hashlib", types.SimpleNamespace(sha256=lf_digest)
        ):
            with self.assertRaisesRegex(ValueError, "missing-venue raw object differs"):
                disposition._missing_venue_raw_object(raw)

    def test_record_normal_assignment_and_validated_scalar_or_bytes_mutations_fail_closed(
        self,
    ) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            MissingVenueDispositionRecord,
        )

        record = self._catalog().for_game_pk(632924)
        for field, replacement in (
            ("game_pk", 1),
            ("projection_index", 1),
            ("_query_canonical_bytes", b"{}"),
            ("_semantics_canonical_bytes", b"{}"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, field, replacement)
        for game_pk, projection_index, query_bytes, semantics_bytes in (
            (1, 2, record._query_canonical_bytes, record._semantics_canonical_bytes),
            (
                632924,
                1,
                record._query_canonical_bytes,
                record._semantics_canonical_bytes,
            ),
            (632924, 2, b"{}", record._semantics_canonical_bytes),
            (632924, 2, record._query_canonical_bytes, b"{}"),
        ):
            with self.subTest(game_pk=game_pk, projection_index=projection_index):
                with self.assertRaises(ValueError):
                    MissingVenueDispositionRecord.from_canonical_bytes(
                        game_pk, projection_index, query_bytes, semantics_bytes
                    )

    def test_records_copy_input_aliases_and_return_fresh_decoded_values(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            MissingVenueDispositionRecord,
        )

        catalog = self._catalog()
        record = catalog.for_game_pk(632924)
        query = record.query
        query_source = {
            "source": query.source,
            "grain": query.grain,
            "endpoint": query.endpoint,
            "parameters": dict(query.parameters),
            "requested_range": dict(query.requested_range),
            "expected_media_type": query.expected_media_type,
            "parser_version": query.parser_version,
            "parent_query_id": query.parent_query_id,
            "run_spec_sha256": query.run_spec_sha256,
            "acquisition_contract_sha256": query.acquisition_contract_sha256,
        }
        semantics_source = json.loads(record._semantics_canonical_bytes)
        copied = MissingVenueDispositionRecord(
            632924,
            2,
            type(query)(**query_source),
            semantics_source,
        )
        query_source["parameters"]["forged"] = True
        semantics_source["raw_object_sha256"] = "0" * 64
        self.assertEqual(logical_query_id(copied.query), logical_query_id(record.query))
        self.assertEqual(copied.semantics, record.semantics)
        first_query, second_query = record.query, record.query
        first_semantics, second_semantics = record.semantics, record.semantics

        self.assertIsNot(first_query, second_query)
        self.assertIsNot(first_semantics, second_semantics)
        with self.assertRaises(TypeError):
            first_query.parameters["forged"] = True
        with self.assertRaises(TypeError):
            first_semantics["forged"] = True
        self.assertEqual(logical_query_id(record.query), logical_query_id(second_query))
        self.assertEqual(record.semantics, second_semantics)

    def test_record_constructor_and_catalog_reject_unsealed_or_mutated_bytes(
        self,
    ) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            MissingVenueDispositionRecord,
            MissingVenueRecoveryDispositionCatalog,
        )

        catalog = self._catalog()
        record = catalog.for_game_pk(632924)
        with self.assertRaises(ValueError):
            MissingVenueDispositionRecord(632924, 1, record.query, record.semantics)
        with self.assertRaises(ValueError):
            MissingVenueDispositionRecord.from_canonical_bytes(
                632924,
                2,
                record._query_canonical_bytes,
                b"{}",
            )
        with self.assertRaises(ValueError):
            MissingVenueRecoveryDispositionCatalog(
                catalog.records,
                catalog.authority_run_spec_sha256,
                catalog.authority_acquisition_contract_sha256,
            ).records[3]._decode_query_bytes(b"{}")


if __name__ == "__main__":
    unittest.main()
