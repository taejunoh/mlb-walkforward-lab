from __future__ import annotations

import copy
import hashlib
import json
import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"
QUERY_SIDECAR_SCHEMA = ROOT / "schemas/v5_2_query_sidecar.schema.json"


def _schedule_snapshot() -> dict[str, object]:
    game = {
        "game_pk": 9001,
        "season": 2024,
        "official_date": "2024-04-02",
        "scheduled_start_utc": "2024-04-02T17:05:00Z",
        "away_team_id": 10,
        "away_score": 2,
        "away_starting_hitter_ids": list(range(1001, 1010)),
        "actual_starter_ids": [101, 201],
        "boxscore_query_sidecar_sha256": "e" * 64,
        "home_team_id": 20,
        "home_score": 4,
        "home_starting_hitter_ids": list(range(2001, 2010)),
        "venue_id": 31,
        "scheduled_innings": 9,
        "completed_innings": 9,
        "terminal_status": "Final",
        "doubleheader_order": 1,
        "verification_status": "VERIFIED",
        "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
        "played": True,
        "neutral_site": False,
        "alternate_site": False,
        "source_game_sha256": "c" * 64,
        "schedule_query_sidecar_sha256": "d" * 64,
        "feature_history_cutoff": "2024-04-01",
        "winner_team_id": 20,
    }
    return {
        "schema_version": 1,
        "grain": "season_game_evidence_snapshot",
        "games": [game],
        "source_query_sidecar_sha256s": ["d" * 64],
    }


SOURCE_SNAPSHOT_SHA256 = "f" * 64
SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
R3_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"


def _r3_catalog() -> object:
    from mlb_backtest.v52_missing_venue_recovery_disposition import (
        R3_ACQUISITION_CONTRACT_RELATIVE,
        R3_RUN_SPEC_RELATIVE,
        build_missing_venue_recovery_disposition_catalog,
    )

    protected_root = Path(
        os.environ["MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT"]
    ).resolve()
    with patch.dict(
        os.environ,
        {"MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT": str(protected_root)},
        clear=False,
    ):
        return build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )


def _r3_schedule_snapshot(game_pk: int) -> dict[str, object]:
    catalog = _r3_catalog()
    record = catalog.for_game_pk(game_pk)
    semantics = record.semantics
    facts = semantics["fixed_game_facts"]
    game = _schedule_snapshot()["games"][0]
    game.update(
        {
            "game_pk": game_pk,
            "season": facts["season"],
            "official_date": facts["official_date"],
            "scheduled_start_utc": semantics["retained_scheduled_start_utc"],
            "terminal_status": facts["terminal_status"],
            "feature_history_cutoff": (
                date.fromisoformat(facts["official_date"]) - timedelta(days=1)
            ).isoformat(),
            "played_date": semantics["played_date"],
            "result_available_on": semantics["result_available_on"],
        }
    )
    return {
        "schema_version": 1,
        "grain": "season_game_evidence_snapshot",
        "games": [game],
        "source_query_sidecar_sha256s": ["d" * 64],
    }


def _task10_source_snapshot() -> dict[str, object]:
    fixture = json.loads(
        (
            ROOT
            / "tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json"
        ).read_text()
    )
    games = []
    for record in fixture["records"]:
        anchor = record["anchor"]
        games.append(
            {
                **anchor,
                "feature_history_cutoff": (
                    date.fromisoformat(anchor["official_date"]) - timedelta(days=1)
                ).isoformat(),
                "normalized_anchor_sha256": record["normalized_anchor_sha256"],
                "source_game_sha256": hashlib.sha256(
                    f"source:{anchor['game_pk']}".encode()
                ).hexdigest(),
                "verification_status": "VERIFIED",
            }
        )
    return {
        "games": games,
        "grain": "season_game_evidence_snapshot",
        "schema_version": 1,
        "source_query_sidecar_sha256s": ["1" * 64],
    }


def _task10_disposition() -> dict[str, object]:
    query_id = "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
    return {
        "query_id": query_id,
        "query_sidecar": {
            "path": (
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "query_sidecars/"
                "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json"
            ),
            "sha256": "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1",
        },
        "success_receipt": {
            "path": (
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                f"mlb_schedule_game_feed/{query_id}/success.json"
            ),
            "sha256": "8" * 64,
        },
    }


class V52HistorySourceTest(unittest.TestCase):
    def test_r3_history_uses_exact_catalog_boundaries_and_compact_mapping(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.history import (
            R3_RECOVERY_RUN_ID,
            r3_history_disposition_mapping,
            recompute_prior_history_sidecar,
        )

        catalog = _r3_catalog()
        projection = catalog.as_projection()
        self.assertEqual(
            r3_history_disposition_mapping(catalog),
            {
                "excluded_game_pks": [632924],
                "projection_query_ids": list(projection.projection_query_ids),
                "record_game_pks": [565534, 630882, 633224, 632924],
                "record_to_projection_indexes": [0, 1, 2, 2],
            },
        )
        for game_pk in (630882, 633224):
            with self.subTest(game_pk=game_pk):
                history = recompute_prior_history_sidecar(
                    _r3_schedule_snapshot(game_pk),
                    source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                    run_id=R3_RECOVERY_RUN_ID,
                    r3_disposition_catalog=catalog,
                )
                self.assertEqual(
                    history["games"][0]["played_date"],
                    catalog.for_game_pk(game_pk).semantics["played_date"],
                )
                self.assertEqual(
                    history["games"][0]["result_available_on"],
                    catalog.for_game_pk(game_pk).semantics["result_available_on"],
                )

    def test_r3_history_rejects_missing_swapped_or_leaked_capabilities(self) -> None:
        from mlb_backtest.v52_sources.history import (
            R3_RECOVERY_RUN_ID,
            recompute_prior_history_sidecar,
        )
        from tests.v52_recovery_schedule_conflict_fixture import (
            published_recovery_capability,
        )

        catalog = _r3_catalog()
        valid = _r3_schedule_snapshot(630882)
        with self.assertRaisesRegex(ValueError, "r3.*catalog|availability"):
            recompute_prior_history_sidecar(
                valid,
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id=R3_RECOVERY_RUN_ID,
            )
        leaked = _r3_schedule_snapshot(630882)
        leaked["games"][0]["game_pk"] = 632924
        with self.assertRaisesRegex(ValueError, "632924|r3"):
            recompute_prior_history_sidecar(
                leaked,
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id=R3_RECOVERY_RUN_ID,
                r3_disposition_catalog=catalog,
            )
        ordinary = _schedule_snapshot()
        ordinary["games"][0].update(
            {"played_date": "2024-04-02", "result_available_on": "2024-04-02"}
        )
        with self.assertRaisesRegex(ValueError, "r3.*availability|unapproved"):
            recompute_prior_history_sidecar(
                ordinary,
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id=R3_RECOVERY_RUN_ID,
                r3_disposition_catalog=catalog,
            )
        with self.assertRaisesRegex(ValueError, "r3.*catalog"):
            recompute_prior_history_sidecar(
                _schedule_snapshot(),
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id="historical_walkforward_500",
                r3_disposition_catalog=catalog,
            )
        with published_recovery_capability() as r2_capability:
            with self.assertRaisesRegex(ValueError, "mutually exclusive|r3"):
                recompute_prior_history_sidecar(
                    valid,
                    source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                    run_id=R3_RECOVERY_RUN_ID,
                    recovery_dispositions=r2_capability,
                    r3_disposition_catalog=catalog,
                )

    def test_statcast_exception_rejects_duck_typed_capability(self) -> None:
        from types import SimpleNamespace

        from mlb_backtest.v52_sources.statcast import (
            validate_recovery_statcast_game_dates,
        )

        class Capability:
            entries = (
                SimpleNamespace(game_pk=565534, played_date=None),
                SimpleNamespace(game_pk=630882, played_date="2020-08-14"),
                SimpleNamespace(game_pk=633224, played_date="2021-09-24"),
            )

            def reauthenticate(self):
                return Capability()

        capability = Capability()
        with self.assertRaisesRegex(ValueError, "availability capability"):
            validate_recovery_statcast_game_dates(
                [{"game_pk": 630882, "game_date": "2020-08-14"}], capability
            )

    def test_public_history_rejects_unbound_availability_fields(self) -> None:
        from mlb_backtest.v52_sources.history import _availability_date

        with self.assertRaisesRegex(ValueError, "unapproved schedule availability"):
            _availability_date(
                {
                    "game_pk": 9001,
                    "official_date": "2021-07-21",
                    "played_date": "2021-07-21",
                    "result_available_on": "2021-07-21",
                },
                None,
            )

    def test_recovery_availability_requires_exact_authenticated_boundary(self) -> None:
        """Changing a resumed-result boundary must keep it out of prior history."""
        from mlb_backtest.v52_sources.history import _availability_date
        from tests.v52_recovery_schedule_conflict_fixture import (
            published_recovery_capability,
        )

        with published_recovery_capability() as capability:
            by_game = {entry.game_pk: entry for entry in capability.entries}
            entry = by_game[630882]
            row = {
                "game_pk": 630882, "official_date": "2020-08-09",
                "scheduled_start_utc": entry.retained_scheduled_start_utc,
                "schedule_query_sidecar_sha256": entry.query_sidecar["sha256"],
                "played_date": entry.played_date,
                "result_available_on": entry.result_available_on,
            }
            self.assertEqual(_availability_date(row, capability), date(2020, 8, 14))
            other = by_game[633224]
            self.assertEqual(_availability_date({
                "game_pk": 633224, "official_date": "2021-07-21",
                "scheduled_start_utc": other.retained_scheduled_start_utc,
                "schedule_query_sidecar_sha256": other.query_sidecar["sha256"],
                "played_date": other.played_date,
                "result_available_on": other.result_available_on,
            }, capability), date(2021, 9, 24))
            with self.assertRaisesRegex(ValueError, "availability boundary"):
                _availability_date({**row, "played_date": "2020-08-13"}, capability)
            with self.assertRaisesRegex(ValueError, "availability boundary"):
                _availability_date({**row, "schedule_query_sidecar_sha256": other.query_sidecar["sha256"]}, capability)

    def test_recovery_statcast_dates_require_authenticated_played_dates(self) -> None:
        from mlb_backtest.v52_sources.statcast import (
            validate_recovery_statcast_game_dates,
        )
        from tests.v52_recovery_schedule_conflict_fixture import published_recovery_capability

        with published_recovery_capability() as capability:
            by_game = {entry.game_pk: entry for entry in capability.entries}
            validate_recovery_statcast_game_dates(
                [{"game_pk": 630882, "game_date": by_game[630882].played_date},
                 {"game_pk": 633224, "game_date": by_game[633224].played_date},
                 {"game_pk": 9001, "game_date": "2021-07-21"}], capability)
            for game_pk, game_date in ((630882, "2020-08-09"), (633224, "2021-09-25")):
                with self.subTest(game_pk=game_pk), self.assertRaisesRegex(ValueError, "played_date"):
                    validate_recovery_statcast_game_dates(
                        [{"game_pk": game_pk, "game_date": game_date}], capability)

    def test_three_exact_duplicate_dependency_neutralizations_and_no_others(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.history import (
            EXPECTED_NEUTRALIZATIONS,
            SOURCE_RUN_ID as history_source_run_id,
            recompute_prior_history_sidecar,
        )

        snapshot = recompute_prior_history_sidecar(
            _task10_source_snapshot(),
            source_snapshot_sha256="1" * 64,
            run_id=SOURCE_RUN_ID,
            duplicate_gamepk_disposition=_task10_disposition(),
        )
        identities = tuple(
            (
                row["target_raw_game_object_sha256"],
                row["target_normalized_anchor_sha256"],
                row["game_pk"],
                row["official_date"],
                row["team_id"],
            )
            for row in snapshot["schedule_neutralizations"]
        )
        self.assertEqual(history_source_run_id, SOURCE_RUN_ID)
        self.assertEqual(identities, EXPECTED_NEUTRALIZATIONS)
        self.assertEqual(len(identities), 3)
        jsonschema.validate(
            snapshot,
            json.loads(SCHEMA.read_text()),
            format_checker=jsonschema.FormatChecker(),
        )
        for row in snapshot["schedule_neutralizations"]:
            self.assertEqual(
                {
                    "value": row["value"],
                    "sample_size": row["sample_size"],
                    "quality": row["quality"],
                    "reason_codes": row["reason_codes"],
                },
                {
                    "value": 50.0,
                    "sample_size": 0,
                    "quality": 0.0,
                    "reason_codes": [
                        "schedule_neutral_duplicate_gamepk_dependency"
                    ],
                },
            )

    def test_schedule_neutralization_mutations_are_rejected(self) -> None:
        from mlb_backtest.v52_sources.history import (
            recompute_prior_history_sidecar,
            validate_schedule_neutralizations,
        )

        baseline = recompute_prior_history_sidecar(
            _task10_source_snapshot(),
            source_snapshot_sha256="1" * 64,
            run_id=SOURCE_RUN_ID,
            duplicate_gamepk_disposition=_task10_disposition(),
        )
        rows = baseline["schedule_neutralizations"]
        attacks = {
            "missing": rows[:-1],
            "duplicate": rows + [rows[0]],
            "changed-hash": [
                {**rows[0], "target_raw_game_object_sha256": "0" * 64},
                *rows[1:],
            ],
            "truncated-hash": [
                {**rows[0], "target_normalized_anchor_sha256": "a" * 63},
                *rows[1:],
            ],
            "reordered": list(reversed(rows)),
            "extra": rows + [{**rows[0], "team_id": 999}],
        }
        for name, value in attacks.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_schedule_neutralizations(value)

        for name, value in attacks.items():
            attack = copy.deepcopy(baseline)
            attack["schedule_neutralizations"] = value
            with self.subTest(schema=name), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(attack, json.loads(SCHEMA.read_text()))

        old = recompute_prior_history_sidecar(
            _schedule_snapshot(),
            source_snapshot_sha256="1" * 64,
            run_id="historical_walkforward_500",
        )
        old["schedule_neutralizations"] = []
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(old, json.loads(SCHEMA.read_text()))

    def test_source_2019_requires_exact_disposition_and_targets(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        dynamic_disposition = copy.deepcopy(_task10_disposition())
        dynamic_disposition["success_receipt"]["sha256"] = "9" * 64
        recompute_prior_history_sidecar(
            _task10_source_snapshot(),
            source_snapshot_sha256="1" * 64,
            run_id=SOURCE_RUN_ID,
            duplicate_gamepk_disposition=dynamic_disposition,
        )
        for mutation in (
            {"query_id": "0" * 64},
            {"query_sidecar": {"path": "wrong", "sha256": "0" * 64}},
            {
                "success_receipt": {
                    "path": "wrong",
                    "sha256": "9" * 64,
                }
            },
            {
                "success_receipt": {
                    "path": _task10_disposition()["success_receipt"]["path"],
                    "sha256": "A" * 64,
                }
            },
        ):
            disposition = copy.deepcopy(_task10_disposition())
            disposition.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                recompute_prior_history_sidecar(
                    _task10_source_snapshot(),
                    source_snapshot_sha256="1" * 64,
                    run_id=SOURCE_RUN_ID,
                    duplicate_gamepk_disposition=disposition,
                )
        for mutate in (
            lambda value: value.update(
                {
                    "games": [
                        game for game in value["games"] if game["game_pk"] != 565338
                    ]
                }
            ),
            lambda value: value["games"].append(copy.deepcopy(value["games"][0])),
            lambda value: value["games"][0].update({"official_date": "2019-05-21"}),
            lambda value: value["games"][0].update({"away_team_id": 999}),
            lambda value: value["games"][0].update(
                {"raw_game_object_sha256": "0" * 64}
            ),
            lambda value: value["games"][0].update(
                {"normalized_anchor_sha256": "0" * 64}
            ),
        ):
            snapshot = _task10_source_snapshot()
            mutate(snapshot)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                recompute_prior_history_sidecar(
                    snapshot,
                    source_snapshot_sha256="1" * 64,
                    run_id=SOURCE_RUN_ID,
                    duplicate_gamepk_disposition=_task10_disposition(),
                )

    def test_source_2019_rejects_unplayed_neighbor_from_another_season(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        snapshot = _task10_source_snapshot()
        neighbor = next(
            game for game in snapshot["games"] if game["game_pk"] == 565535
        )
        neighbor.update({"season": 2020, "verification_status": "INELIGIBLE_UNPLAYED"})
        neighbor.pop("raw_game_object_sha256")
        neighbor.pop("normalized_anchor_sha256")
        with self.assertRaisesRegex(ValueError, "one season"):
            recompute_prior_history_sidecar(
                snapshot,
                source_snapshot_sha256="1" * 64,
                run_id=SOURCE_RUN_ID,
                duplicate_gamepk_disposition=_task10_disposition(),
            )

    def test_565535_and_566157_complete_hashes_are_known_but_unapproved(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.history import (
            KNOWN_UNAPPROVED_NEIGHBORS,
            recompute_prior_history_sidecar,
        )

        source = _task10_source_snapshot()
        snapshot = recompute_prior_history_sidecar(
            source,
            source_snapshot_sha256="1" * 64,
            run_id=SOURCE_RUN_ID,
            duplicate_gamepk_disposition=_task10_disposition(),
        )
        input_identities = {
            (
                game["raw_game_object_sha256"],
                game["normalized_anchor_sha256"],
                game["game_pk"],
                game["official_date"],
                team_id,
            )
            for game in source["games"]
            for team_id in (game["away_team_id"], game["home_team_id"])
        }
        emitted = {
            (
                row["target_raw_game_object_sha256"],
                row["target_normalized_anchor_sha256"],
                row["game_pk"],
                row["official_date"],
                row["team_id"],
            )
            for row in snapshot["schedule_neutralizations"]
        }
        self.assertTrue(set(KNOWN_UNAPPROVED_NEIGHBORS).issubset(input_identities))
        self.assertFalse(set(KNOWN_UNAPPROVED_NEIGHBORS) & emitted)

    def test_legacy_history_bytes_are_unchanged(self) -> None:
        from mlb_backtest.v52_source_contracts import canonical_json_bytes
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        old = recompute_prior_history_sidecar(
            _schedule_snapshot(),
            source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
            run_id="historical_walkforward_500",
        )
        self.assertNotIn("schedule_neutralizations", old)
        # Base canonical payload bytes exclude the publisher's trailing newline.
        encoded = canonical_json_bytes(old)
        self.assertEqual(len(encoded), 687)
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "b4803935007402aa248928f01b06ba9f4d4c18463fdec3d574fa8b586578f941",
        )
        self.assertEqual(len(encoded + b"\n"), 688)

    def test_runtime_matrix_closes_source_fields_and_disposition(self) -> None:
        from mlb_backtest.v52_source_contracts import canonical_sha256
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        sourced = _task10_source_snapshot()
        with self.assertRaises(ValueError):
            recompute_prior_history_sidecar(
                sourced,
                source_snapshot_sha256="1" * 64,
                run_id="historical_walkforward_500",
            )
        for game in sourced["games"]:
            game["season"] = 2020
            anchor = {
                key: game[key]
                for key in (
                    "alternate_site",
                    "away_score",
                    "away_team_id",
                    "completed_innings",
                    "doubleheader_order",
                    "game_pk",
                    "home_score",
                    "home_team_id",
                    "neutral_site",
                    "official_date",
                    "played",
                    "raw_game_object_sha256",
                    "scheduled_innings",
                    "scheduled_start_utc",
                    "season",
                    "season_start_date",
                    "terminal_status",
                    "venue_id",
                    "winner_team_id",
                )
            }
            game["normalized_anchor_sha256"] = canonical_sha256(anchor)
        source_2020 = recompute_prior_history_sidecar(
            sourced,
            source_snapshot_sha256="1" * 64,
            run_id=SOURCE_RUN_ID,
        )
        self.assertNotIn("schedule_neutralizations", source_2020)
        with self.assertRaises(ValueError):
            recompute_prior_history_sidecar(
                sourced,
                source_snapshot_sha256="1" * 64,
                run_id=SOURCE_RUN_ID,
                duplicate_gamepk_disposition=_task10_disposition(),
            )
        sourced["games"][0].pop("normalized_anchor_sha256")
        with self.assertRaises(ValueError):
            recompute_prior_history_sidecar(
                sourced,
                source_snapshot_sha256="1" * 64,
                run_id=SOURCE_RUN_ID,
            )
    def test_prior_history_binds_every_source_game_hash_and_d1_cutoff(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        snapshot = recompute_prior_history_sidecar(
            _schedule_snapshot(),
            source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
            run_id="historical_walkforward_500",
        )
        jsonschema.validate(
            snapshot,
            json.loads(SCHEMA.read_text()),
            format_checker=jsonschema.FormatChecker(),
        )
        self.assertEqual(snapshot["grain"], "verified_game_projection")
        self.assertEqual(snapshot["source_game_sha256s"], ["c" * 64])
        self.assertEqual(snapshot["games"][0]["history_cutoff"], "2024-04-01")
        self.assertRegex(snapshot["games"][0]["projection_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(snapshot["source_snapshot_sha256"], SOURCE_SNAPSHOT_SHA256)
        self.assertEqual(
            snapshot,
            recompute_prior_history_sidecar(
                _schedule_snapshot(),
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id="historical_walkforward_500",
            ),
        )

    def test_prior_history_requires_complete_upstream_snapshot_digest(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        with self.assertRaises(TypeError):
            recompute_prior_history_sidecar(_schedule_snapshot())
        for digest in ("F" * 64, "f" * 63, None):
            with self.subTest(digest=digest), self.assertRaisesRegex(
                ValueError, "source snapshot"
            ):
                recompute_prior_history_sidecar(
                    _schedule_snapshot(),
                    source_snapshot_sha256=digest,  # type: ignore[arg-type]
                    run_id="historical_walkforward_500",
                )

    def test_prior_history_rejects_missing_hash_non_d1_and_unverified_game(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        for mutation, message in (({"source_game_sha256": None}, "source game hash"), ({"feature_history_cutoff": "2024-04-02"}, "D-1"), ({"verification_status": "BLOCKED"}, "verified")):
            snapshot = copy.deepcopy(_schedule_snapshot())
            snapshot["games"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, message):
                recompute_prior_history_sidecar(
                    snapshot,
                    source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                    run_id="historical_walkforward_500",
                )

    def test_prior_history_rejects_noncanonical_or_non_utc_start_time(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        for scheduled_start in (
            "not-a-date-time",
            "2024-04-02T13:05:00-04:00",
        ):
            snapshot = copy.deepcopy(_schedule_snapshot())
            snapshot["games"][0]["scheduled_start_utc"] = scheduled_start
            with self.subTest(scheduled_start=scheduled_start), self.assertRaisesRegex(
                ValueError, "scheduled_start_utc"
            ):
                recompute_prior_history_sidecar(
                    snapshot,
                    source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                    run_id="historical_walkforward_500",
                )

    def test_history_schema_requires_canonical_utc_start_time(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        schema = json.loads(SCHEMA.read_text())
        baseline = recompute_prior_history_sidecar(
            _schedule_snapshot(),
            source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
            run_id="historical_walkforward_500",
        )
        for scheduled_start in (
            "not-a-date-time",
            "2024-04-02T13:05:00-04:00",
        ):
            attack = copy.deepcopy(baseline)
            attack["games"][0]["scheduled_start_utc"] = scheduled_start
            with self.subTest(scheduled_start=scheduled_start), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(
                    attack,
                    schema,
                    format_checker=jsonschema.FormatChecker(),
                )

    def test_season_snapshot_schema_is_closed_and_grain_specific(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        self.assertIn("oneOf", schema)
        encoded = json.dumps(schema, sort_keys=True)
        self.assertNotIn('"row_count"', encoded)
        snapshot = _schedule_snapshot()
        snapshot["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(snapshot, schema)
        incomplete = _schedule_snapshot()
        del incomplete["games"][0]["actual_starter_ids"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(incomplete, schema)

    def test_schedule_and_history_aggregates_reject_empty_populations(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        empty_schedule = {
            "games": [],
            "grain": "season_game_evidence_snapshot",
            "schema_version": 1,
            "source_query_sidecar_sha256s": [],
        }
        with self.assertRaisesRegex(ValueError, "nonempty"):
            recompute_prior_history_sidecar(
                empty_schedule,
                source_snapshot_sha256=SOURCE_SNAPSHOT_SHA256,
                run_id="historical_walkforward_500",
            )
        schema = json.loads(SCHEMA.read_text())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(empty_schedule, schema)
        empty_history = {
            "games": [],
            "grain": "verified_game_projection",
            "schema_version": 1,
            "source_game_sha256s": [],
            "source_snapshot_sha256": "a" * 64,
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(empty_history, schema)

    def test_dispatch_returns_task2_compatible_query_envelope(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar
        from mlb_backtest.v52_evidence import LogicalQuery

        raw = (ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv").read_bytes()
        query = LogicalQuery(
            source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            parameters={"all": "true", "type": "details", "player_type": "batter", "game_date_gt": "2024-04-01", "game_date_lt": "2024-04-30", "hfGT": "R|", "group_by": "name-year", "sort_col": "pitches", "player_event_sort": "api_p_release_speed", "sort_order": "desc", "min_pitches": "0"},
            requested_range={"year": 2024, "start_date": "2024-04-01", "end_date": "2024-04-30"},
            expected_media_type="text/csv", parser_version="v5.2.1",
            parent_query_id=None, run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = recompute_query_sidecar(query, raw)
        self.assertNotIn("semantics", semantics)
        from mlb_backtest.v52_source_contracts import query_sidecar

        published_shape = query_sidecar(raw, query, semantics)
        jsonschema.validate(
            published_shape, json.loads(QUERY_SIDECAR_SCHEMA.read_text())
        )
        self.assertEqual(published_shape["semantics"], semantics)
        self.assertEqual(
            published_shape["raw_sha256"], hashlib.sha256(raw).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
