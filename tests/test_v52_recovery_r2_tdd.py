from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest.protocols import parse_source_run_spec
from tests.v52_recovery_schedule_conflict_fixture import recovery_authority_fixture


ROOT = Path(__file__).resolve().parents[1]
R2_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_r2"
R2_RUN = Path(
    "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json"
)
R2_CONTRACT = Path(
    "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json"
)
R2_RAW = Path("data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw")


def _r2_source_run():
    from mlb_backtest.protocols import expected_recovery_source_run

    raw = expected_recovery_source_run()
    return parse_source_run_spec(raw)


class RecoveryR2TddTests(unittest.TestCase):
    def test_recovery_r2_schema_accepts_only_the_new_authority(self) -> None:
        from mlb_backtest.protocols import expected_recovery_source_run, parse_source_run_spec

        raw = expected_recovery_source_run()
        parsed = parse_source_run_spec(raw)
        self.assertEqual(parsed.run_id, R2_RUN_ID)

    def test_selector_routes_r2_and_forged_source_fails_closed(self) -> None:
        from mlb_backtest import v52_acquire

        r2 = _r2_source_run()
        self.assertEqual(
            v52_acquire._authority_paths_for_run_spec(r2, ROOT),
            (ROOT / R2_RUN, ROOT / R2_CONTRACT),
        )
        forged = dataclasses.replace(r2, run_id=r2.run_id + "_forged")
        with self.assertRaises(TypeError):
            v52_acquire._authority_paths_for_run_spec(forged, ROOT)

    def test_real_runtime_selects_r2_design_plan_and_contract(self) -> None:
        from mlb_backtest import v52_acquire

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = v52_acquire._AcquisitionRequest(
                root=root,
                run_spec=_r2_source_run(),
                years=range(2018, 2025),
                mode="hybrid",
                resume=False,
                stop_after=None,
                legacy_root=root / "data/v5_1_bt/raw",
                protected_inventory_path=root / "audit/v5_1_bt/protected_raw_inventory.json",
                reserve_bytes=0,
                plan_only=False,
                acquisition_contract_path=root / R2_CONTRACT,
            )
            with patch(
                "mlb_backtest.execution_registry.initialize_execution_registry",
                return_value=SimpleNamespace(phase="ACQUIRING"),
            ) as initialize:
                v52_acquire._real_begin_runtime(
                    request,
                    v52_acquire._AcquisitionPlan("a" * 64, ("b" * 64,), ()),
                    v52_acquire._RuntimeState(None, False),
                )
            self.assertEqual(initialize.call_args.kwargs["design_path"], root / Path("docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md"))
            self.assertEqual(initialize.call_args.kwargs["plan_path"], root / Path("docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md"))
            self.assertEqual(initialize.call_args.kwargs["acquisition_contract_path"], root / R2_CONTRACT)

    def test_canonical_raw_parent_uses_r2_namespace(self) -> None:
        from mlb_backtest import v52_acquire

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = dataclasses.replace(
                v52_acquire._AcquisitionRequest(
                    root=root, run_spec=_r2_source_run(), years=range(2018, 2025), mode="hybrid", resume=False, stop_after=None,
                    legacy_root=root / "legacy", protected_inventory_path=root / "protected.json", reserve_bytes=0, plan_only=False,
                )
            )
            self.assertEqual(v52_acquire._ensure_canonical_raw_parent(request), root / R2_RAW)

    def test_timezone_gate_uses_r2_authority_pair(self) -> None:
        from mlb_backtest import v52_acquire

        with (
            patch("mlb_backtest.v52_acquire._required_gate_leaf_exists", return_value=True),
            patch("mlb_backtest.v52_timezone_review.load_verified_timezone_gate") as load,
        ):
            v52_acquire._load_required_timezone_gate(ROOT, _r2_source_run())
        self.assertEqual(load.call_args.kwargs["run_spec_path"], ROOT / R2_RUN)
        self.assertEqual(load.call_args.kwargs["acquisition_contract_path"], ROOT / R2_CONTRACT)

    def test_recovery_r2_registry_authority_pair_is_exact(self) -> None:
        from mlb_backtest import execution_registry

        self.assertEqual(execution_registry._RECOVERY_RUN_ID, R2_RUN_ID)
        self.assertEqual(execution_registry._RECOVERY_CONTRACT_PATH, R2_CONTRACT.as_posix())

    def test_orchestration_verify_and_timezone_use_r2_authority(self) -> None:
        from mlb_backtest import v52_source_orchestration, v52_timezone_review, v52_verify_sources

        self.assertEqual(v52_source_orchestration._RECOVERY_RUN_ID, R2_RUN_ID)
        self.assertEqual(v52_verify_sources._RECOVERY_RUN_ID, R2_RUN_ID)
        self.assertEqual(v52_timezone_review._RECOVERY_RUN_SPEC_PATH, R2_RUN)
        self.assertEqual(v52_timezone_review._RECOVERY_ACQUISITION_CONTRACT_PATH, R2_CONTRACT)
        self.assertEqual(v52_timezone_review._RECOVERY_ACQUISITION_AUDIT / "venue_timezones.json", Path("audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/venue_timezones.json"))

    def test_disposition_and_source_orchestration_reject_r1_authority(self) -> None:
        from mlb_backtest import v52_schedule_conflict_recovery_disposition, v52_source_orchestration

        self.assertEqual(v52_schedule_conflict_recovery_disposition._RECOVERY_RUN_ID, R2_RUN_ID)
        self.assertEqual(v52_source_orchestration._RECOVERY_RUN_ID, R2_RUN_ID)
        self.assertNotEqual(v52_schedule_conflict_recovery_disposition._RECOVERY_RUN_ID, "historical_walkforward_500_recovery_schedule_conflicts")

    def test_recovery_disposition_catalog_uses_exact_r2_authority_pair(self) -> None:
        from mlb_backtest import v52_acquire
        from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id

        queries = tuple(
            LogicalQuery(
                source="mlb_schedule_game_feed",
                grain="season_schedule_and_game",
                endpoint=endpoint,
                parameters={},
                requested_range={"year": 2020 + index},
                expected_media_type="application/json",
                parser_version="v5.2.2",
                parent_query_id=None,
                run_spec_sha256="a" * 64,
                acquisition_contract_sha256="b" * 64,
            )
            for index, endpoint in enumerate(
                (
                    "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
                    "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
                    "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
                )
            )
        )
        request = v52_acquire._AcquisitionRequest(
            root=ROOT,
            run_spec=_r2_source_run(),
            years=range(2018, 2025),
            mode="hybrid",
            resume=False,
            stop_after=None,
            legacy_root=ROOT / "data/v5_1_bt/raw",
            protected_inventory_path=ROOT / "audit/v5_1_bt/protected_raw_inventory.json",
            reserve_bytes=0,
            plan_only=True,
        )
        authorities = tuple(SimpleNamespace(query=query) for query in queries)
        with patch(
            "mlb_backtest.v52_schedule_conflict_recovery_disposition."
            "build_recovery_schedule_conflict_catalog",
            return_value=SimpleNamespace(entries=authorities),
        ) as build_catalog:
            selected = v52_acquire._source_disposition_authorities(
                request, (SimpleNamespace(queries=queries),)
            )
        self.assertEqual(
            selected,
            {logical_query_id(query): authority for query, authority in zip(queries, authorities)},
        )
        self.assertEqual(
            build_catalog.call_args.args,
            (ROOT, ROOT / R2_RUN, ROOT / R2_CONTRACT),
        )

    def test_r2_disposition_public_contract_remains_three_records_and_queries(self) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            RECOVERY_DISPOSITION_ENDPOINTS,
            RecoveryDispositionCatalog,
            RecoveryDispositionProjection,
            build_recovery_schedule_conflict_catalog,
        )

        self.assertEqual(
            RECOVERY_DISPOSITION_ENDPOINTS,
            (
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
            ),
        )
        self.assertEqual(RecoveryDispositionCatalog.__dataclass_fields__["entries"].type, "tuple[RecoveryDispositionAuthority, ...]")
        with tempfile.TemporaryDirectory() as directory:
            repository, run_spec, contract = recovery_authority_fixture(
                Path(directory)
            )
            projection = build_recovery_schedule_conflict_catalog(
                repository, run_spec, contract
            ).as_projection()
        self.assertEqual(projection.record_game_pks, (565534, 630882, 633224))
        self.assertIs(type(projection), RecoveryDispositionProjection)
        self.assertEqual(projection.record_to_projection_indexes, (0, 1, 2))
        self.assertEqual(projection.excluded_game_pks, frozenset())
        self.assertEqual(
            projection.projection_query_ids,
            (
                "1c8fd832cd18285d3ff35b1920ec9e0d1073550132e11235c636f404531711db",
                "5ea730594c017b0cde008b3a9a8826355ea0f00724a7c768693f50259306ea14",
                "6f40def4a04d8373f45dcea125b2100cb684415e3f7b14f5a737646d548cb1c9",
            ),
        )

    def test_projection_constructor_and_lookup_reject_noncanonical_scalars(
        self,
    ) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionProjection,
        )

        game_pks = (565534, 630882, 633224, 632924)
        query_ids = ("a" * 64, "b" * 64, "c" * 64)
        indexes = (0, 1, 2, 2)
        for query_id in ("arbitrary", "A" * 64, "a" * 63):
            with self.subTest(query_id=query_id):
                with self.assertRaises(ValueError):
                    RecoveryDispositionProjection(
                        game_pks,
                        (query_id, *query_ids[1:]),
                        indexes,
                        frozenset({632924}),
                    )
        for excluded_game_pk in (632924.0, True, 0, -1, "632924"):
            with self.subTest(excluded_game_pk=excluded_game_pk):
                with self.assertRaises(ValueError):
                    RecoveryDispositionProjection(
                        game_pks,
                        query_ids,
                        indexes,
                        frozenset({excluded_game_pk}),
                    )

        projection = RecoveryDispositionProjection(
            game_pks, query_ids, indexes, frozenset({632924})
        )
        for game_pk in (632924.0, True, 0, -1, "632924"):
            with self.subTest(game_pk=game_pk):
                with self.assertRaises(ValueError):
                    projection.projection_query_id_for_game_pk(game_pk)

    def test_finalization_accepts_only_the_r2_authority_path_pair(self) -> None:
        from mlb_backtest import v52_source_orchestration as orchestration

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context = orchestration.FinalizationContext(
                root, root / R2_RUN, root / R2_CONTRACT,
                root / "audit/v5_1_bt/protected_raw_inventory.json", _r2_source_run(),
                None, None, None, None, "ABSENT", None, (), None,
            )
            self.assertEqual(
                orchestration._exact_authority_branch(context),
                "recovery_schedule_conflicts",
            )
            old_context = dataclasses.replace(
                context,
                run_spec_path=root / "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json",
                acquisition_contract_path=root / "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json",
            )
            with self.assertRaises(orchestration.FinalizationError):
                orchestration._exact_authority_branch(old_context)

    def test_verify_sources_uses_exact_r2_authority_pair_and_batch_regex(self) -> None:
        from mlb_backtest import v52_verify_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            expected_context = SimpleNamespace(repository_root=root, authority=_r2_source_run())
            with patch.object(
                v52_verify_sources, "_load_static_contracts", return_value=expected_context
            ) as load_static:
                self.assertIs(
                    v52_verify_sources._authenticate_authority_paths(
                        root, _r2_source_run(), root / R2_CONTRACT
                    ),
                    expected_context,
                )
            self.assertEqual(load_static.call_args.args, (root / R2_RUN, root / R2_CONTRACT))

        r2_batch = (
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/"
            "acquisition/query_batches/" + "a" * 64 + ".json"
        )
        r1_batch = (
            "audit/v5_2_qd/historical_recovery_schedule_conflicts/"
            "acquisition/query_batches/" + "a" * 64 + ".json"
        )
        self.assertIsNotNone(v52_verify_sources._STATCAST_ROOT_BATCH_PATH.fullmatch(r2_batch))
        self.assertIsNone(v52_verify_sources._STATCAST_ROOT_BATCH_PATH.fullmatch(r1_batch))

    def test_verify_sources_rejects_non_r2_source_before_selecting_superseding_path(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources

        forged = dataclasses.replace(
            _r2_source_run(),
            run_id="historical_walkforward_500_superseding_565534",
        )
        with patch.object(v52_verify_sources, "_load_static_contracts") as load_static:
            with self.assertRaisesRegex(ValueError, "authority object differs"):
                v52_verify_sources._authenticate_authority_paths(
                    ROOT, forged, ROOT / R2_CONTRACT
                )
        load_static.assert_not_called()


if __name__ == "__main__":
    unittest.main()
