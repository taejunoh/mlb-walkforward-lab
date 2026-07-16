from __future__ import annotations

import builtins
import copy
import dataclasses
import hashlib
import json
import socket
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_seasons import SEASON_BOUNDARIES
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_sources.schedule import canonical_sha256
from tests.test_v52_verify_sources import _normalized_game_authority


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


if __name__ == "__main__":
    unittest.main()
