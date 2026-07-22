from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from mlb_backtest.protocols import load_run_spec, load_source_run_spec
from mlb_backtest.v52_evidence import AttemptCapture, V52EvidenceStore
from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch
from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC_PATH = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT_PATH = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
SCHEMA_PATH = ROOT / "schemas/v5_2_schedule_anchor_inventory.schema.json"
INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
SOURCE_RUN_SPEC_PATH = (
    ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
SOURCE_CONTRACT_PATH = (
    ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
_SOURCE_AUTHORITY_FILES = (
    RUN_SPEC_PATH.relative_to(ROOT),
    CONTRACT_PATH.relative_to(ROOT),
    SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
    SOURCE_CONTRACT_PATH.relative_to(ROOT),
    Path("schemas/v5_2_acquisition_contract.schema.json"),
    Path(
        "docs/superpowers/specs/"
        "2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
    ),
    Path(
        "docs/superpowers/plans/"
        "2026-07-13-mlb-v5-2-hybrid-acquisition.md"
    ),
    Path(
        "docs/superpowers/specs/"
        "2026-07-14-mlb-v5-2-plan-a-contract-amendment.md"
    ),
    Path(
        "docs/superpowers/specs/"
        "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md"
    ),
    Path(
        "docs/superpowers/plans/"
        "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md"
    ),
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _capture(raw: bytes) -> AttemptCapture:
    return AttemptCapture(
        started_at_utc="2026-07-14T12:00:00.000001Z",
        ended_at_utc="2026-07-14T12:00:01.000001Z",
        final_url="https://statsapi.mlb.com/api/v1/schedule",
        http_status=200,
        response_headers=(
            ("content-type", "application/json"),
            ("content-length", str(len(raw))),
        ),
        body=raw,
        error_classification=None,
    )


def _schedule_raw(query, index: int, *, game_pk: int | None = None) -> bytes:
    season = int(query.requested_range["year"])
    official_date = str(query.requested_range["start_date"])
    game_pk = season * 100 + index + 1 if game_pk is None else game_pk
    played = index % 2 == 0
    away: dict[str, object] = {"team": {"id": 10 + index, "name": "Away"}}
    home: dict[str, object] = {"team": {"id": 20 + index, "name": "Home"}}
    if played:
        away.update({"score": 2, "isWinner": False})
        home.update({"score": 4, "isWinner": True})
    game: dict[str, object] = {
        "gamePk": game_pk,
        "gameType": "R",
        "gameDate": f"{official_date}T17:05:00Z",
        "officialDate": official_date,
        "doubleHeader": "N",
        "gameNumber": 1,
        "scheduledInnings": 9,
        "status": {"detailedState": "Final" if played else "Postponed"},
        "teams": {"away": away, "home": home},
        "venue": {"id": 100 + index, "name": "Park"},
        "neutralSite": index == 1,
        "isAlternateSite": index == 1,
    }
    if played:
        game["linescore"] = {
            "currentInning": 9,
            "inningHalf": "Bottom",
            "isTopInning": False,
            "outs": 3,
        }
    return _canonical(
        {
            "dates": [
                {"date": official_date, "games": [game], "totalGames": 1}
            ],
            "totalGames": 1,
        }
    )


@dataclasses.dataclass(frozen=True)
class _Fixture:
    root: Path
    evidence_root: Path
    run_spec: object
    batch_id: str


def _fixture(
    base: Path,
    *,
    duplicate_game_pk: bool = False,
    missing_success: int | None = None,
) -> _Fixture:
    root = base / "repo"
    protocol_dir = root / "protocols/v5_2_qd"
    protocol_dir.mkdir(parents=True)
    (protocol_dir / RUN_SPEC_PATH.name).write_bytes(RUN_SPEC_PATH.read_bytes())
    (protocol_dir / CONTRACT_PATH.name).write_bytes(CONTRACT_PATH.read_bytes())
    run_spec = load_run_spec(protocol_dir / RUN_SPEC_PATH.name)
    evidence_root = root / str(run_spec.raw["namespaces"]["raw"])
    evidence_root.mkdir(parents=True)
    batch = build_static_batch(RUN_SPEC_PATH, CONTRACT_PATH)
    publish_query_batch(root, batch)
    store = V52EvidenceStore(evidence_root)
    for index, query in enumerate(batch.queries):
        if index == missing_success:
            continue
        raw = _schedule_raw(
            query,
            index,
            game_pk=777_777 if duplicate_game_pk else None,
        )
        sidecar = recompute_schedule_anchor_sidecar(raw, query)
        with store.acquire_query(query) as lease:
            store.publish_fetched(
                query,
                store.next_attempt_number(lease),
                _capture(raw),
                sidecar["semantics"],
            )
    return _Fixture(root, evidence_root, run_spec, batch.batch_id)


def _kwargs(fixture: _Fixture) -> dict[str, object]:
    return {
        "root": fixture.root,
        "run_spec": fixture.run_spec,
        "acquisition_contract_path": fixture.root
        / "protocols/v5_2_qd/historical_source_acquisition.json",
        "static_batch_id": fixture.batch_id,
        "evidence_root": fixture.evidence_root,
    }


class V52ScheduleInventoryTest(unittest.TestCase):
    def test_source_authority_rejects_wrong_raw_root_and_cross_pair_before_evidence(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            build_schedule_anchor_inventory,
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "repo"
            for relative in _SOURCE_AUTHORITY_FILES:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "schedule@example.test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Schedule Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "authority"], cwd=root, check=True
            )
            source_run_path = root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT)
            source_contract_path = root / SOURCE_CONTRACT_PATH.relative_to(ROOT)
            source_run = load_source_run_spec(source_run_path)
            wrong_root = root / "data/v5_2_qd/historical/raw"
            with self.assertRaisesRegex(ValueError, "evidence root"):
                build_schedule_anchor_inventory(
                    root,
                    source_run,
                    acquisition_contract_path=source_contract_path,
                    static_batch_id="0" * 64,
                    evidence_root=wrong_root,
                )
            source_raw = (
                root / "data/v5_2_qd/historical_superseding_565534/raw"
            )
            with (
                patch(
                    "mlb_backtest.v52_schedule_inventory.load_query_batch",
                    side_effect=AssertionError("query-batch I/O reached"),
                ) as loader,
                self.assertRaisesRegex(ValueError, "Task 6"),
            ):
                build_schedule_anchor_inventory(
                    root,
                    source_run,
                    acquisition_contract_path=source_contract_path,
                    static_batch_id="0" * 64,
                    evidence_root=source_raw,
                )
            loader.assert_not_called()
            with (
                patch(
                    "mlb_backtest.v52_schedule_inventory.build_schedule_anchor_inventory",
                    side_effect=AssertionError("inventory build reached"),
                ) as builder,
                self.assertRaisesRegex(ValueError, "Task 6"),
            ):
                publish_schedule_anchor_inventory(
                    root,
                    source_run,
                    acquisition_contract_path=source_contract_path,
                    static_batch_id="0" * 64,
                    evidence_root=source_raw,
                )
            builder.assert_not_called()
            with (
                patch(
                    "mlb_backtest.v52_schedule_inventory._read_inventory_contents",
                    side_effect=AssertionError("inventory read reached"),
                ) as inventory_reader,
                self.assertRaisesRegex(ValueError, "Task 6"),
            ):
                load_verified_schedule_anchor_inventory(
                    root,
                    source_run,
                    acquisition_contract_path=source_contract_path,
                    static_batch_id="0" * 64,
                    evidence_root=source_raw,
                )
            inventory_reader.assert_not_called()
            with self.assertRaises(ValueError):
                build_schedule_anchor_inventory(
                    root,
                    source_run,
                    acquisition_contract_path=root
                    / "protocols/v5_2_qd/historical_source_acquisition.json",
                    static_batch_id="0" * 64,
                    evidence_root=source_raw,
                )

    def test_build_publish_load_are_deterministic_frozen_and_schema_valid(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            VerifiedScheduleAnchorInventory,
            build_schedule_anchor_inventory,
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            payload = build_schedule_anchor_inventory(**_kwargs(fixture))
            self.assertEqual(
                set(payload),
                {
                    "acquisition_contract",
                    "anchors",
                    "artifact_kind",
                    "counts",
                    "run_id",
                    "run_spec",
                    "schema_version",
                    "season_sources",
                    "static_batch",
                    "team_ids",
                    "venue_ids",
                },
            )
            self.assertEqual(payload["artifact_kind"], "SCHEDULE_ANCHOR_INVENTORY")
            self.assertEqual(payload["run_id"], "historical_walkforward_500")
            self.assertEqual([row["season"] for row in payload["season_sources"]], list(range(2018, 2025)))
            self.assertEqual([row["season"] for row in payload["anchors"]], list(range(2018, 2025)))
            self.assertEqual(payload["counts"], {
                "game_count": 7,
                "played_game_count": 4,
                "season_count": 7,
                "unplayed_game_count": 3,
            })
            jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_bytes()))

            published = publish_schedule_anchor_inventory(**_kwargs(fixture))
            self.assertIsInstance(published, VerifiedScheduleAnchorInventory)
            self.assertTrue(dataclasses.is_dataclass(published))
            self.assertTrue(published.__dataclass_params__.frozen)
            self.assertEqual(published.path, fixture.root / INVENTORY_RELATIVE)
            self.assertEqual(_thaw(published.payload), payload)
            with self.assertRaises(TypeError):
                published.payload["anchors"][0]["game_pk"] = 999_999_999
            self.assertEqual(published.sha256, hashlib.sha256(published.path.read_bytes()).hexdigest())
            first_bytes = published.path.read_bytes()
            self.assertEqual(publish_schedule_anchor_inventory(**_kwargs(fixture)), published)
            self.assertEqual(published.path.read_bytes(), first_bytes)
            self.assertEqual(load_verified_schedule_anchor_inventory(**_kwargs(fixture)), published)

    def test_missing_success_and_wrong_evidence_root_fail_closed(self) -> None:
        from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_inventory

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve(), missing_success=3)
            with self.assertRaisesRegex((ValueError, RuntimeError), "success|SUCCESS"):
                build_schedule_anchor_inventory(**_kwargs(fixture))
            wrong = fixture.root / "wrong-raw"
            wrong.mkdir()
            with self.assertRaisesRegex(ValueError, "evidence root|raw namespace"):
                build_schedule_anchor_inventory(**{**_kwargs(fixture), "evidence_root": wrong})

    def test_forged_sidecar_raw_and_saved_payload_are_detected(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        for mutation in ("sidecar", "raw", "saved"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                fixture = _fixture(Path(td).resolve())
                published = publish_schedule_anchor_inventory(**_kwargs(fixture))
                query_dir = next((fixture.evidence_root / "queries/mlb_schedule_game_feed").iterdir())
                if mutation == "sidecar":
                    target = next((query_dir / "sidecars").iterdir())
                    target.write_bytes(target.read_bytes().replace(b'"schema_version":1', b'"schema_version":2', 1))
                elif mutation == "raw":
                    target = next((query_dir / "objects").iterdir())
                    target.write_bytes(target.read_bytes().replace(b'"totalGames":1', b'"totalGames":2', 1))
                else:
                    payload = json.loads(published.path.read_bytes())
                    payload["counts"]["game_count"] = 8
                    published.path.write_bytes(_canonical(payload))
                with self.assertRaises((ValueError, RuntimeError)):
                    load_verified_schedule_anchor_inventory(**_kwargs(fixture))

    def test_saved_reorder_duplicate_and_derived_sets_are_rejected(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        for mutation in ("reorder", "duplicate", "team", "venue"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                fixture = _fixture(Path(td).resolve())
                published = publish_schedule_anchor_inventory(**_kwargs(fixture))
                payload = json.loads(published.path.read_bytes())
                if mutation == "reorder":
                    payload["anchors"][0], payload["anchors"][1] = payload["anchors"][1], payload["anchors"][0]
                elif mutation == "duplicate":
                    payload["anchors"][1]["game_pk"] = payload["anchors"][0]["game_pk"]
                elif mutation == "team":
                    payload["team_ids"].append(999)
                else:
                    payload["venue_ids"].append(999)
                published.path.write_bytes(_canonical(payload))
                with self.assertRaises(ValueError):
                    load_verified_schedule_anchor_inventory(**_kwargs(fixture))

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve(), duplicate_game_pk=True)
            from mlb_backtest.v52_schedule_inventory import (
                build_schedule_anchor_inventory,
            )

            with self.assertRaisesRegex(ValueError, "gamePk|ordered|unique"):
                build_schedule_anchor_inventory(**_kwargs(fixture))

    def test_fixed_path_rejects_symlink_hardlink_and_conflicting_existing_bytes(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        for mutation in ("symlink", "hardlink", "conflict"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                fixture = _fixture(Path(td).resolve())
                final = fixture.root / INVENTORY_RELATIVE
                final.parent.mkdir(parents=True, exist_ok=True)
                target = fixture.root / "target.json"
                target.write_bytes(b"{}\n")
                if mutation == "symlink":
                    final.symlink_to(target)
                elif mutation == "hardlink":
                    os.link(target, final)
                else:
                    final.write_bytes(b"{}\n")
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_schedule_anchor_inventory(**_kwargs(fixture))
                with self.assertRaises((ValueError, RuntimeError)):
                    load_verified_schedule_anchor_inventory(**_kwargs(fixture))

    def test_concurrent_identical_publication_converges(self) -> None:
        from mlb_backtest.v52_schedule_inventory import publish_schedule_anchor_inventory

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(lambda _: publish_schedule_anchor_inventory(**_kwargs(fixture)), range(2)))
            self.assertEqual(values[0], values[1])
            self.assertEqual(values[0].path.stat().st_nlink, 1)

    def test_prepublication_evidence_mutation_leaves_no_final_artifact(self) -> None:
        import mlb_backtest.v52_schedule_inventory as inventory

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            query_dir = next(
                (fixture.evidence_root / "queries/mlb_schedule_game_feed").iterdir()
            )
            raw_path = next((query_dir / "objects").iterdir())

            def mutate_verified_raw() -> None:
                raw_path.write_bytes(
                    raw_path.read_bytes().replace(
                        b'"totalGames":1', b'"totalGames":2', 1
                    )
                )

            with (
                patch.object(
                    inventory,
                    "_before_schedule_inventory_publish",
                    side_effect=mutate_verified_raw,
                ),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                inventory.publish_schedule_anchor_inventory(**_kwargs(fixture))
            self.assertFalse((fixture.root / INVENTORY_RELATIVE).exists())

    def test_post_rebind_evidence_mutation_removes_owned_final_artifact(self) -> None:
        import mlb_backtest.v52_schedule_inventory as inventory

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            query_dir = next(
                (fixture.evidence_root / "queries/mlb_schedule_game_feed").iterdir()
            )
            raw_path = next((query_dir / "objects").iterdir())

            def mutate_after_rebind() -> None:
                raw_path.write_bytes(
                    raw_path.read_bytes().replace(
                        b'"totalGames":1', b'"totalGames":2', 1
                    )
                )

            with (
                patch.object(
                    inventory,
                    "_after_schedule_inventory_rebind",
                    side_effect=mutate_after_rebind,
                ),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                inventory.publish_schedule_anchor_inventory(**_kwargs(fixture))
            self.assertFalse((fixture.root / INVENTORY_RELATIVE).exists())

    def test_post_rebind_preexisting_winner_is_never_removed(self) -> None:
        import mlb_backtest.v52_schedule_inventory as inventory

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            winner = _canonical(
                inventory.build_schedule_anchor_inventory(**_kwargs(fixture))
            )
            final = fixture.root / INVENTORY_RELATIVE
            query_dir = next(
                (fixture.evidence_root / "queries/mlb_schedule_game_feed").iterdir()
            )
            raw_path = next((query_dir / "objects").iterdir())

            def install_winner_then_mutate() -> None:
                final.write_bytes(winner)
                raw_path.write_bytes(
                    raw_path.read_bytes().replace(
                        b'"totalGames":1', b'"totalGames":2', 1
                    )
                )

            with (
                patch.object(
                    inventory,
                    "_after_schedule_inventory_rebind",
                    side_effect=install_winner_then_mutate,
                ),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                inventory.publish_schedule_anchor_inventory(**_kwargs(fixture))
            self.assertEqual(final.read_bytes(), winner)

    def test_verified_loader_rejects_oversized_artifact_before_parsing(self) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
        )

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            final = fixture.root / INVENTORY_RELATIVE
            final.parent.mkdir(parents=True, exist_ok=True)
            with final.open("wb") as stream:
                from mlb_backtest import v52_schedule_inventory as inventory

                stream.truncate(inventory._MAX_INVENTORY_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "bounded|limit|exceeds"):
                load_verified_schedule_anchor_inventory(**_kwargs(fixture))

    def test_schema_is_canonical_closed_and_rejects_extra_keys(self) -> None:
        schema_bytes = SCHEMA_PATH.read_bytes()
        schema = json.loads(schema_bytes)
        self.assertEqual(schema_bytes, _canonical(schema))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_inventory

            payload = copy.deepcopy(build_schedule_anchor_inventory(**_kwargs(fixture)))
            payload["extra"] = True
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(payload, schema)
            valid = build_schedule_anchor_inventory(**_kwargs(fixture))
            validator = Draft202012Validator(
                schema, format_checker=FormatChecker()
            )
            bad_date = copy.deepcopy(valid)
            bad_date["anchors"][0]["official_date"] = "2018-02-30"
            self.assertFalse(validator.is_valid(bad_date))
            bad_path = copy.deepcopy(valid)
            bad_path["run_spec"]["path"] = "audit/../escape.json"
            self.assertFalse(validator.is_valid(bad_path))


if __name__ == "__main__":
    unittest.main()
