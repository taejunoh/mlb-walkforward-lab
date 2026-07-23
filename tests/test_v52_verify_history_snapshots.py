from __future__ import annotations

import builtins
import copy
import dataclasses
import hashlib
import json
import socket
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_seasons import SEASON_BOUNDARIES
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_sources.schedule import canonical_sha256
from tests.test_v52_source_history import _task10_disposition, _task10_source_snapshot
from tests.test_v52_verify_sources import (
    _authenticated_source_game_authority,
    _normalized_game_authority,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
RUN_ID = "historical_walkforward_500"


def _game_snapshots():
    from mlb_backtest import v52_verify_sources as verifier

    authority = _normalized_game_authority()
    originals = verifier._build_game_normalized_snapshots(
        RUN_ID,
        authority.records,
        authority.game_inventory,
        authority.timezone_gate,
        authority.schedule_inventory,
    )
    snapshots = []
    for original in originals:
        mapping = _mutable(original)
        opening, closing = SEASON_BOUNDARIES[original.season]
        for game in mapping["payload"]["games"]:
            official = game["official_date"]
            if not opening.isoformat() <= official <= closing.isoformat():
                official = opening.isoformat()
                game["official_date"] = official
                game["scheduled_start_utc"] = f"{official}T17:05:00Z"
            game["season_start_date"] = opening.isoformat()
            game["feature_history_cutoff"] = (
                date.fromisoformat(official) - timedelta(days=1)
            ).isoformat()
            game["source_game_sha256"] = _source_game_hash(game)
        mapping["payload"]["games"].sort(
            key=lambda row: (row["official_date"], row["game_pk"])
        )
        snapshots.append(_rebuild(mapping))
    return tuple(snapshots)


def _rebuild(mapping: dict[str, object]):
    from mlb_backtest import v52_verify_sources as verifier

    contents = canonical_json_bytes(mapping) + b"\n"
    disposition_value = mapping.get("duplicate_gamepk_disposition")
    disposition = None
    if isinstance(disposition_value, dict):
        disposition = verifier.DuplicateGamePkDispositionBinding(
            query_id=disposition_value["query_id"],
            success_receipt=verifier.AuthorityBinding(
                **disposition_value["success_receipt"]
            ),
            query_sidecar=verifier.AuthorityBinding(
                **disposition_value["query_sidecar"]
            ),
        )
    return verifier.NormalizedSnapshot(
        schema_version=1,
        run_id=str(mapping["run_id"]),
        season=int(mapping["season"]),
        source=str(mapping["source"]),  # type: ignore[arg-type]
        grain=str(mapping["grain"]),  # type: ignore[arg-type]
        status="READY",
        logical_query_ids=tuple(mapping["logical_query_ids"]),
        source_query_sidecar_sha256s=tuple(
            mapping["source_query_sidecar_sha256s"]
        ),
        split_receipt_sha256s=tuple(mapping["split_receipt_sha256s"]),
        payload=mapping["payload"],
        canonical_bytes=contents,
        sha256=hashlib.sha256(contents).hexdigest(),
        duplicate_gamepk_disposition=disposition,
        dispositioned_query_ids=tuple(mapping.get("dispositioned_query_ids", ())),
    )


def _mutable(snapshot) -> dict[str, object]:
    return json.loads(canonical_json_bytes(snapshot.as_mapping()))


def _source_game_hash(game: dict[str, object]) -> str:
    projected = dict(game)
    projected.pop("source_game_sha256", None)
    lineage: dict[str, object] = {
        "anchor_sidecar_sha256": projected["schedule_query_sidecar_sha256"],
        "game": projected,
    }
    if projected["played"] is True:
        lineage["boxscore_query_sidecar_sha256"] = projected[
            "boxscore_query_sidecar_sha256"
        ]
    return canonical_sha256(lineage)


def _source_game_snapshots():
    """Build source-run game envelopes with the sealed neutralization anchors."""
    from mlb_backtest import v52_verify_sources as verifier

    authority = _authenticated_source_game_authority()
    originals = verifier._build_game_normalized_snapshots(
        "historical_walkforward_500_superseding_565534",
        authority.records,
        authority.game_inventory,
        authority.timezone_gate,
        authority.schedule_inventory,
        duplicate_gamepk_disposition=authority.binding,
        dispositioned_query_ids=(authority.disposition_id,),
    )
    disposition_value = _task10_disposition()
    authority.binding = verifier.DuplicateGamePkDispositionBinding(
        query_id=disposition_value["query_id"],
        success_receipt=verifier.AuthorityBinding(
            **disposition_value["success_receipt"]
        ),
        query_sidecar=verifier.AuthorityBinding(**disposition_value["query_sidecar"]),
    )
    authority.disposition_id = authority.binding.query_id
    snapshots = []
    for original in originals:
        mapping = _mutable(original)
        if original.season == 2019:
            mapping["duplicate_gamepk_disposition"] = authority.binding.as_mapping()
            mapping["dispositioned_query_ids"] = [authority.disposition_id]
            template = dict(mapping["payload"]["games"][0])
            games = []
            for anchor in _task10_source_snapshot()["games"]:
                game = {
                    **template,
                    **anchor,
                    "boxscore_query_sidecar_sha256": template[
                        "boxscore_query_sidecar_sha256"
                    ],
                    "participant_provenance": template["participant_provenance"],
                    "actual_starter_ids": template["actual_starter_ids"],
                    "away_starting_hitter_ids": template[
                        "away_starting_hitter_ids"
                    ],
                    "home_starting_hitter_ids": template[
                        "home_starting_hitter_ids"
                    ],
                }
                game["source_game_sha256"] = _source_game_hash(game)
                games.append(game)
            mapping["payload"]["games"] = sorted(
                games, key=lambda row: (row["official_date"], row["game_pk"])
            )
        else:
            for game in mapping["payload"]["games"]:
                game["game_pk"] += original.season * 1_000_000
        opening, closing = SEASON_BOUNDARIES[original.season]
        for game in mapping["payload"]["games"]:
            official = game["official_date"]
            if not opening.isoformat() <= official <= closing.isoformat():
                official = opening.isoformat()
                game["official_date"] = official
                game["scheduled_start_utc"] = f"{official}T17:05:00Z"
            game["season_start_date"] = opening.isoformat()
            game["feature_history_cutoff"] = (
                date.fromisoformat(official) - timedelta(days=1)
            ).isoformat()
            if game["played"]:
                anchor_keys = (
                    "alternate_site", "away_score", "away_team_id",
                    "completed_innings", "doubleheader_order", "game_pk",
                    "home_score", "home_team_id", "neutral_site", "official_date",
                    "played", "raw_game_object_sha256", "scheduled_innings",
                    "scheduled_start_utc", "season", "season_start_date",
                    "terminal_status", "venue_id", "winner_team_id",
                )
                game["normalized_anchor_sha256"] = canonical_sha256(
                    {key: game[key] for key in anchor_keys}
                )
            game["source_game_sha256"] = _source_game_hash(game)
        snapshots.append(_rebuild(mapping))
    return tuple(snapshots), authority


class V52HistoryNormalizedSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.games = _game_snapshots()

    def _build(self, games=None):
        from mlb_backtest import v52_verify_sources as verifier

        return verifier._build_history_normalized_snapshots(
            RUN_ID, self.games if games is None else games
        )

    def test_builds_exactly_seven_ordered_ready_history_snapshots(self) -> None:
        snapshots = self._build()

        self.assertEqual([snapshot.season for snapshot in snapshots], list(range(2018, 2025)))
        self.assertEqual(len(snapshots), 7)
        for history, game in zip(snapshots, self.games, strict=True):
            self.assertEqual(history.source, "prior_schedule_history")
            self.assertEqual(history.grain, "verified_game_projection")
            self.assertEqual(history.status, "READY")
            self.assertEqual(history.payload["source_snapshot_sha256"], game.sha256)
            self.assertNotEqual(
                history.payload["source_snapshot_sha256"],
                canonical_sha256(dict(game.payload)),
            )
            self.assertEqual(history.logical_query_ids, game.logical_query_ids)
            self.assertEqual(
                history.source_query_sidecar_sha256s,
                game.source_query_sidecar_sha256s,
            )
            self.assertEqual(history.split_receipt_sha256s, ())
            self.assertEqual(
                {row["game_pk"] for row in history.payload["games"]},
                {row["game_pk"] for row in game.payload["games"]},
            )
            self.assertEqual(
                {row["terminal_status"] for row in history.payload["games"]},
                {row["terminal_status"] for row in game.payload["games"]},
            )

    def test_is_deterministic_schema_valid_and_uses_no_io(self) -> None:
        first = self._build()
        schema = json.loads(SCHEMA.read_text())
        for snapshot in first:
            jsonschema.validate(snapshot.as_mapping(), schema)

        with (
            patch.object(Path, "open", side_effect=AssertionError("filesystem access")),
            patch.object(builtins, "open", side_effect=AssertionError("filesystem access")),
            patch.object(socket, "socket", side_effect=AssertionError("network access")),
        ):
            second = self._build()
        self.assertEqual(
            [(item.canonical_bytes, item.sha256) for item in first],
            [(item.canonical_bytes, item.sha256) for item in second],
        )

    def test_calls_history_reducer_exactly_once_per_season(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        with patch.object(
            verifier,
            "recompute_prior_history_sidecar",
            wraps=verifier.recompute_prior_history_sidecar,
        ) as reducer:
            snapshots = self._build()

        self.assertEqual(len(snapshots), 7)
        self.assertEqual(reducer.call_count, 7)

    def test_rejects_missing_duplicate_extra_and_2025_populations(self) -> None:
        extra = dataclasses.replace(
            self.games[-1],
            season=2025,
            canonical_bytes=self.games[-1].canonical_bytes,
        )
        populations = (
            self.games[:-1],
            self.games + (self.games[-1],),
            self.games + (extra,),
        )
        for values in populations:
            with self.subTest(seasons=[item.season for item in values]), self.assertRaises(
                (TypeError, ValueError)
            ):
                self._build(values)

    def test_rejects_game_pk_reused_across_seasons(self) -> None:
        values = list(self.games)
        reused_game_pk = values[0].payload["games"][0]["game_pk"]
        mapping = _mutable(values[1])
        game = mapping["payload"]["games"][0]
        game["game_pk"] = reused_game_pk
        game["source_game_sha256"] = _source_game_hash(game)
        mapping["payload"]["games"].sort(
            key=lambda row: (row["official_date"], row["game_pk"])
        )
        values[1] = _rebuild(mapping)

        with self.assertRaisesRegex(ValueError, "gamePk"):
            self._build(tuple(values))

    def test_rejects_forged_upstream_identity_and_old_payload_hash(self) -> None:
        original = self.games[0]
        for forged in (
            dataclasses.replace(original, canonical_bytes=original.canonical_bytes + b" "),
            dataclasses.replace(
                original,
                sha256=canonical_sha256(
                    json.loads(canonical_json_bytes(original.as_mapping())[0:])[
                        "payload"
                    ]
                ),
            ),
            dataclasses.replace(original, source="prior_schedule_history"),
            dataclasses.replace(original, grain="verified_game_projection"),
            dataclasses.replace(original, run_id="another-run"),
            dataclasses.replace(original, season=2019),
        ):
            values = (forged,) + self.games[1:]
            with self.subTest(forged=forged), self.assertRaises((TypeError, ValueError)):
                self._build(values)

    def test_preserves_2020_frozen_boundary_and_opening_day_d_minus_one(self) -> None:
        values = list(self.games)
        index = 2
        mapping = _mutable(values[index])
        payload = mapping["payload"]
        game = payload["games"][0]
        opening, _closing = SEASON_BOUNDARIES[2020]
        game["official_date"] = opening.isoformat()
        game["feature_history_cutoff"] = (opening - timedelta(days=1)).isoformat()
        game["scheduled_start_utc"] = f"{opening.isoformat()}T17:05:00Z"
        game["source_game_sha256"] = _source_game_hash(game)
        payload["games"].sort(key=lambda row: (row["official_date"], row["game_pk"]))
        values[index] = _rebuild(mapping)

        history = self._build(tuple(values))[index]
        row = next(item for item in history.payload["games"] if item["game_pk"] == game["game_pk"])
        self.assertEqual(row["official_date"], "2020-07-23")
        self.assertEqual(row["history_cutoff"], "2020-07-22")

    def test_rejects_game_dates_outside_frozen_season_boundaries(self) -> None:
        mapping = _mutable(self.games[0])
        payload = mapping["payload"]
        game = payload["games"][-1]
        _opening, closing = SEASON_BOUNDARIES[2018]
        outside = closing + timedelta(days=1)
        game["official_date"] = outside.isoformat()
        game["feature_history_cutoff"] = (outside - timedelta(days=1)).isoformat()
        game["scheduled_start_utc"] = f"{outside.isoformat()}T17:05:00Z"
        game["source_game_sha256"] = _source_game_hash(game)
        payload["games"].sort(key=lambda row: (row["official_date"], row["game_pk"]))
        forged = _rebuild(mapping)

        with self.assertRaisesRegex(ValueError, "bound|season"):
            self._build((forged,) + self.games[1:])

    def test_history_mapping_rejects_lineage_and_projection_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        baseline = self._build()[0].as_mapping()
        attacks: list[tuple[str, dict[str, object]]] = []

        nonempty_receipt = copy.deepcopy(baseline)
        nonempty_receipt["split_receipt_sha256s"] = ["a" * 64]
        attacks.append(("receipt", nonempty_receipt))

        bad_order = copy.deepcopy(baseline)
        bad_order["payload"]["games"].reverse()
        attacks.append(("order", bad_order))

        duplicate_pk = copy.deepcopy(baseline)
        duplicate_pk["payload"]["games"].append(
            copy.deepcopy(duplicate_pk["payload"]["games"][0])
        )
        attacks.append(("gamePk", duplicate_pk))

        bad_projection = copy.deepcopy(baseline)
        bad_projection["payload"]["games"][0]["projection_sha256"] = "b" * 64
        attacks.append(("projection", bad_projection))

        bad_cutoff = copy.deepcopy(baseline)
        bad_cutoff["payload"]["games"][0]["history_cutoff"] = bad_cutoff[
            "payload"
        ]["games"][0]["official_date"]
        attacks.append(("D-1", bad_cutoff))

        duplicate_hash = copy.deepcopy(baseline)
        duplicate_hash["payload"]["source_game_sha256s"] = [
            duplicate_hash["payload"]["source_game_sha256s"][0],
            duplicate_hash["payload"]["source_game_sha256s"][0],
        ]
        attacks.append(("source hashes", duplicate_hash))

        for label, attack in attacks:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                verifier._validate_normalized_snapshot_mapping(attack)

    def test_history_mapping_rejects_same_game_pk_on_different_dates(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        attack = self._build()[0].as_mapping()
        first, second = attack["payload"]["games"]
        second["game_pk"] = first["game_pk"]
        later = date.fromisoformat(first["official_date"]) + timedelta(days=1)
        second["official_date"] = later.isoformat()
        second["history_cutoff"] = (later - timedelta(days=1)).isoformat()
        second["source_game_sha256"] = "f" * 64
        projected = dict(second)
        projected.pop("projection_sha256")
        second["projection_sha256"] = canonical_sha256(projected)
        attack["payload"]["games"].sort(
            key=lambda row: (row["official_date"], row["game_pk"])
        )
        attack["payload"]["source_game_sha256s"] = sorted(
            row["source_game_sha256"] for row in attack["payload"]["games"]
        )

        with self.assertRaisesRegex(ValueError, "gamePk"):
            verifier._validate_normalized_snapshot_mapping(attack)

    def test_runtime_and_normalized_schema_reject_non_utc_start_time(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        schema = json.loads(SCHEMA.read_text())
        baseline = self._build()[0].as_mapping()
        for scheduled_start in (
            "not-a-date-time",
            "2018-04-02T13:05:00-04:00",
        ):
            attack = copy.deepcopy(baseline)
            row = attack["payload"]["games"][0]
            row["scheduled_start_utc"] = scheduled_start
            projected = dict(row)
            projected.pop("projection_sha256")
            row["projection_sha256"] = canonical_sha256(projected)
            with self.subTest(
                layer="runtime", scheduled_start=scheduled_start
            ), self.assertRaisesRegex(ValueError, "scheduled start"):
                verifier._validate_normalized_snapshot_mapping(attack)
            with self.subTest(
                layer="schema", scheduled_start=scheduled_start
            ), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    attack,
                    schema,
                    format_checker=jsonschema.FormatChecker(),
                )

    def test_exact_replay_rejects_old_hash_and_transitive_array_changes(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        game = self.games[0]
        history = self._build()[0]
        self.assertEqual(
            verifier._validate_history_snapshot_lineage(history, game),
            history.as_mapping(),
        )
        attacks = []

        old_hash = history.as_mapping()
        old_hash["payload"]["source_snapshot_sha256"] = canonical_sha256(
            _mutable(game)["payload"]
        )
        attacks.append(old_hash)

        subset = history.as_mapping()
        subset["logical_query_ids"] = subset["logical_query_ids"][:1]
        subset["source_query_sidecar_sha256s"] = subset[
            "source_query_sidecar_sha256s"
        ][:1]
        attacks.append(subset)

        superset = history.as_mapping()
        superset["logical_query_ids"].append("a" * 64)
        superset["source_query_sidecar_sha256s"].append("b" * 64)
        attacks.append(superset)

        for attack in attacks:
            forged = _rebuild(attack)
            with self.assertRaisesRegex(ValueError, "lineage"):
                verifier._validate_history_snapshot_lineage(forged, game)

    def test_source_2019_history_replays_authenticated_neutralizations(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.v52_sources.history import EXPECTED_NEUTRALIZATIONS

        games, authority = _source_game_snapshots()
        histories = verifier._build_history_normalized_snapshots(
            "historical_walkforward_500_superseding_565534",
            games,
            duplicate_gamepk_disposition=authority.binding,
            dispositioned_query_ids=(authority.disposition_id,),
        )
        source_game = next(snapshot for snapshot in games if snapshot.season == 2019)
        source_history = next(
            snapshot for snapshot in histories if snapshot.season == 2019
        )
        self.assertEqual(
            tuple(
                (
                    row["target_raw_game_object_sha256"],
                    row["target_normalized_anchor_sha256"],
                    row["game_pk"],
                    row["official_date"],
                    row["team_id"],
                )
                for row in source_history.payload["schedule_neutralizations"]
            ),
            EXPECTED_NEUTRALIZATIONS,
        )
        self.assertEqual(
            verifier._validate_history_snapshot_lineage(source_history, source_game),
            source_history.as_mapping(),
        )
        self.assertEqual(
            source_history.duplicate_gamepk_disposition,
            authority.binding,
        )
        self.assertEqual(source_history.dispositioned_query_ids, (authority.disposition_id,))
        jsonschema.validate(
            source_history.as_mapping(),
            json.loads(SCHEMA.read_text()),
            format_checker=jsonschema.FormatChecker(),
        )
        self.assertNotIn(
            "schedule_neutralizations",
            next(snapshot for snapshot in histories if snapshot.season == 2020).payload,
        )

    def test_history_runtime_closes_source_disposition_and_neutralizations(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        games, authority = _source_game_snapshots()
        history = next(
            snapshot
            for snapshot in verifier._build_history_normalized_snapshots(
                "historical_walkforward_500_superseding_565534",
                games,
                duplicate_gamepk_disposition=authority.binding,
                dispositioned_query_ids=(authority.disposition_id,),
            )
            if snapshot.season == 2019
        )
        for label, mutate in (
            ("run", lambda value: value.update({"run_id": RUN_ID})),
            (
                "query",
                lambda value: value.update(
                    {"dispositioned_query_ids": ["0" * 64]}
                ),
            ),
            (
                "path",
                lambda value: value["duplicate_gamepk_disposition"]["query_sidecar"].update(
                    {"path": "wrong"}
                ),
            ),
            (
                "sha",
                lambda value: value["duplicate_gamepk_disposition"]["query_sidecar"].update(
                    {"sha256": "0" * 64}
                ),
            ),
            (
                "missing",
                lambda value: value["payload"].pop("schedule_neutralizations"),
            ),
            (
                "empty",
                lambda value: value["payload"].update(
                    {"schedule_neutralizations": []}
                ),
            ),
            (
                "raw",
                lambda value: value["payload"]["schedule_neutralizations"][0].update(
                    {"target_raw_game_object_sha256": "0" * 64}
                ),
            ),
            (
                "anchor",
                lambda value: value["payload"]["schedule_neutralizations"][0].update(
                    {"target_normalized_anchor_sha256": "0" * 64}
                ),
            ),
            (
                "reordered",
                lambda value: value["payload"]["schedule_neutralizations"].reverse(),
            ),
            (
                "extra",
                lambda value: value["payload"]["schedule_neutralizations"].append(
                    copy.deepcopy(value["payload"]["schedule_neutralizations"][0])
                ),
            ),
        ):
            attack = _mutable(history)
            mutate(attack)
            with self.subTest(label=label), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)
            with self.subTest(schema=label), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(
                    attack,
                    json.loads(SCHEMA.read_text()),
                    format_checker=jsonschema.FormatChecker(),
                )

        other_source = next(
            snapshot
            for snapshot in verifier._build_history_normalized_snapshots(
                "historical_walkforward_500_superseding_565534",
                games,
                duplicate_gamepk_disposition=authority.binding,
                dispositioned_query_ids=(authority.disposition_id,),
            )
            if snapshot.season == 2020
        ).as_mapping()
        other_source["payload"]["schedule_neutralizations"] = []
        with self.assertRaises(ValueError):
            verifier._validate_normalized_snapshot_mapping(other_source)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                other_source,
                json.loads(SCHEMA.read_text()),
                format_checker=jsonschema.FormatChecker(),
            )

        old = self._build()[0]
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("I/O")),
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("I/O")),
        ):
            self.assertEqual(
                verifier._validate_history_snapshot_lineage(old, self.games[0]),
                old.as_mapping(),
            )
        self.assertNotIn("schedule_neutralizations", old.payload)
        mixed = old.as_mapping()
        mixed["payload"]["schedule_neutralizations"] = []
        mixed["duplicate_gamepk_disposition"] = _task10_disposition()
        mixed["dispositioned_query_ids"] = [
            mixed["duplicate_gamepk_disposition"]["query_id"]
        ]
        with self.assertRaises(ValueError):
            verifier._validate_normalized_snapshot_mapping(mixed)


if __name__ == "__main__":
    unittest.main()
