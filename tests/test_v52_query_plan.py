from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    V52EvidenceStore,
    canonical_query_bytes,
    logical_query_id,
)
from mlb_backtest.statcast import statcast_url
from mlb_backtest.protocols import RunSpec, SourceRunSpec
from mlb_backtest.v52_seasons import SEASON_BOUNDARIES


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
SOURCE_RUN_SPEC = (
    ROOT
    / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
SOURCE_CONTRACT = (
    ROOT
    / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
SOURCE_ADDENDUM = Path(
    "docs/superpowers/specs/"
    "2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md"
)
RUN_SHA = hashlib.sha256(RUN_SPEC.read_bytes()).hexdigest()
CONTRACT_SHA = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()

_STATCAST_AUTHORITY_FILES = (
    Path("protocols/v5_2_qd/historical_walkforward_500.json"),
    Path("protocols/v5_2_qd/historical_source_acquisition.json"),
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
    Path("schemas/v5_2_acquisition_contract.schema.json"),
    Path("protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"),
    Path(
        "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
    ),
    Path(
        "docs/superpowers/specs/"
        "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md"
    ),
    Path(
        "docs/superpowers/plans/"
        "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md"
    ),
    SOURCE_ADDENDUM,
)


def _statcast_authority_fixture(parent: Path, *, in_place: bool = False) -> Path:
    root = (parent if in_place else parent / "repo").resolve()
    for relative in _STATCAST_AUTHORITY_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "query-plan@example.test"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Query Plan Test"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture authority"], cwd=root, check=True
    )
    return root


def _inventory_anchor(**changes: object) -> dict[str, object]:
    season = int(changes.get("season", 2024))
    first, _ = SEASON_BOUNDARIES[season]
    index = season - 2018
    played = changes.get("played", False)
    anchor: dict[str, object] = {
        "alternate_site": False,
        "away_score": 2 if played is True else None,
        "away_team_id": 10 + index,
        "completed_innings": 9 if played is True else None,
        "doubleheader_order": 1,
        "game_pk": season * 100 + index + 1,
        "home_score": 4 if played is True else None,
        "home_team_id": 20 + index,
        "neutral_site": False,
        "official_date": first.isoformat(),
        "played": played,
        "scheduled_innings": 9,
        "scheduled_start_utc": f"{first.isoformat()}T17:05:00Z",
        "season": season,
        "season_start_date": first.isoformat(),
        "terminal_status": "Final" if played is True else "Postponed",
        "venue_id": 100 + index,
        "winner_team_id": 20 + index if played is True else None,
    }
    anchor.update(changes)
    return anchor


def _verified_schedule_inventory(
    custom_anchors: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], str]:
    by_season: dict[int, list[dict[str, object]]] = {}
    for raw in custom_anchors or []:
        anchor = _inventory_anchor(**raw)
        by_season.setdefault(int(anchor["season"]), []).append(anchor)
    anchors = [
        anchor
        for season in range(2018, 2025)
        for anchor in by_season.get(season, [_inventory_anchor(season=season)])
    ]
    anchors.sort(key=lambda row: (int(row["season"]), int(row["game_pk"])))
    season_sources = []
    for season in range(2018, 2025):
        rows = [row for row in anchors if row["season"] == season]
        played = sum(row["played"] is True for row in rows)
        first, last = SEASON_BOUNDARIES[season]
        digest = hashlib.sha256(str(season).encode()).hexdigest()
        season_sources.append(
            {
                "game_count": len(rows),
                "inclusive_end": last.isoformat(),
                "inclusive_start": first.isoformat(),
                "played_game_count": played,
                "query_id": digest,
                "query_sidecar": {
                    "path": f"data/v5_2_qd/historical/raw/{season}.sidecar.json",
                    "sha256": digest,
                },
                "raw_sha256": digest,
                "season": season,
                "storage_mode": "FETCHED_RAW",
                "success_receipt": {
                    "path": f"data/v5_2_qd/historical/raw/{season}.success.json",
                    "sha256": digest,
                },
                "unplayed_game_count": len(rows) - played,
            }
        )
    played_count = sum(row["played"] is True for row in anchors)
    payload: dict[str, object] = {
        "acquisition_contract": {
            "path": "protocols/v5_2_qd/historical_source_acquisition.json",
            "sha256": CONTRACT_SHA,
        },
        "anchors": anchors,
        "artifact_kind": "SCHEDULE_ANCHOR_INVENTORY",
        "counts": {
            "game_count": len(anchors),
            "played_game_count": played_count,
            "season_count": 7,
            "unplayed_game_count": len(anchors) - played_count,
        },
        "run_id": "historical_walkforward_500",
        "run_spec": {
            "path": "protocols/v5_2_qd/historical_walkforward_500.json",
            "sha256": RUN_SHA,
        },
        "schema_version": 1,
        "season_sources": season_sources,
        "static_batch": {
            "batch_id": "d" * 64,
            "path": "audit/v5_2_qd/historical/acquisition/query_batches/"
            + "d" * 64
            + ".json",
            "sha256": "e" * 64,
        },
        "team_ids": sorted(
            {
                int(row[field])
                for row in anchors
                for field in ("away_team_id", "home_team_id")
            }
        ),
        "venue_ids": sorted({int(row["venue_id"]) for row in anchors}),
    }
    return payload, hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
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


def _write_forged_canonical_batch(
    root: Path,
    *,
    stage: str,
    derived_from_sha256: str | None,
    queries: tuple[LogicalQuery, ...],
) -> str:
    payload = {
        "stage": stage,
        "derived_from_sha256": derived_from_sha256,
        "queries": [json.loads(canonical_query_bytes(query)) for query in queries],
    }
    batch_id = hashlib.sha256(
        _canonical_json_bytes(payload).removesuffix(b"\n")
    ).hexdigest()
    record = {"schema_version": 1, "batch_id": batch_id, **payload}
    batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / f"{batch_id}.json").write_bytes(_canonical_json_bytes(record))
    return batch_id


def _participant_authority_fixture(root: Path):
    from mlb_backtest.v52_game_evidence_inventory import (
        publish_game_evidence_inventory,
    )
    from tests.test_v52_game_evidence_inventory import _fixture

    _fixture(root)
    return publish_game_evidence_inventory(root)


def _forged_participant_batch(
    valid,
    *,
    derived_from_sha256: str | None = None,
    queries: tuple[LogicalQuery, ...] | None = None,
):
    from mlb_backtest.v52_query_plan import QueryBatch

    forged_queries = valid.queries if queries is None else queries
    derived = (
        valid.derived_from_sha256
        if derived_from_sha256 is None
        else derived_from_sha256
    )
    payload = {
        "derived_from_sha256": derived,
        "queries": [
            json.loads(canonical_query_bytes(query)) for query in forged_queries
        ],
        "stage": "PARTICIPANT_DERIVED",
    }
    batch_id = hashlib.sha256(
        _canonical_json_bytes(payload).removesuffix(b"\n")
    ).hexdigest()
    return QueryBatch(
        batch_id=batch_id,
        stage="PARTICIPANT_DERIVED",
        derived_from_sha256=derived,
        queries=forged_queries,
    )


def _parent(start: str, end: str) -> LogicalQuery:
    query = LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={
            "all": "true",
            "type": "details",
            "player_type": "batter",
            "game_date_gt": start,
            "game_date_lt": end,
            "hfGT": "R|",
            "group_by": "name-year",
            "sort_col": "pitches",
            "player_event_sort": "api_p_release_speed",
            "sort_order": "desc",
            "min_pitches": "0",
        },
        requested_range={
            "year": int(start[:4]),
            "start_date": start,
            "end_date": end,
        },
        expected_media_type="text/csv",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )
    return query


def _legacy(start: str, end: str, **extra: object) -> dict[str, object]:
    return {
        "source": "baseball_savant_statcast",
        "rawstore_verified": True,
        "canonical_query": {
            "start": start,
            "end": end,
            "game_type": "R",
            "url": statcast_url(start, end),
        },
        **extra,
    }


def _split_capture(query: LogicalQuery, reason: str) -> AttemptCapture:
    if reason == "TIMEOUT":
        return AttemptCapture(
            started_at_utc="2026-07-14T12:00:00.000000Z",
            ended_at_utc="2026-07-14T12:01:00.000000Z",
            final_url=None,
            http_status=None,
            response_headers=(),
            body=None,
            error_classification="TRANSIENT_NETWORK",
            timed_out=True,
        )
    return AttemptCapture(
        started_at_utc="2026-07-14T12:00:00.000000Z",
        ended_at_utc="2026-07-14T12:01:00.000000Z",
        final_url=query.endpoint,
        http_status=200,
        response_headers=(("Content-Type", query.expected_media_type),),
        body=b"authenticated partial split response",
        error_classification=reason,
    )


def _record_split_attempt(root: Path, query: LogicalQuery, reason: str) -> int:
    store = V52EvidenceStore(_split_evidence_root(root))
    with store.acquire_query(query) as lease:
        attempt_number = store.next_attempt_number(lease)
        store.persist_split_attempt(
            query,
            attempt_number,
            _split_capture(query, reason),
            reason=reason,
        )
    return attempt_number


def _forge_generic_split_attempt(
    root: Path, query: LogicalQuery, reason: str
) -> int:
    import mlb_backtest.v52_evidence as evidence

    store = V52EvidenceStore(_split_evidence_root(root))
    with store.acquire_query(query) as lease:
        attempt_number = store.next_attempt_number(lease)
        record = evidence._attempt_record(
            logical_query_id(query),
            attempt_number,
            _split_capture(query, "TIMEOUT"),
            "SPLIT_REQUIRED",
            error_classification=reason,
        )
        path = (
            store.root
            / "queries"
            / query.source
            / logical_query_id(query)
            / "attempts"
            / f"{attempt_number:06d}.json"
        )
        path.write_bytes(_canonical_json_bytes(record))
    return attempt_number


def _split_evidence_root(repository_root: Path) -> Path:
    run_spec_path = (
        repository_root / "protocols/v5_2_qd/historical_walkforward_500.json"
    )
    run_spec_path.parent.mkdir(parents=True, exist_ok=True)
    if not run_spec_path.exists():
        run_spec_path.write_bytes(RUN_SPEC.read_bytes())
    contract_path = (
        repository_root / "protocols/v5_2_qd/historical_source_acquisition.json"
    )
    if not contract_path.exists():
        contract_path.write_bytes(CONTRACT.read_bytes())
    raw = repository_root / str(
        json.loads(run_spec_path.read_bytes())["namespaces"]["raw"]
    )
    raw.mkdir(parents=True, exist_ok=True)
    return raw


def _split_batches(root: Path):
    import mlb_backtest.v52_query_plan as query_plan

    _statcast_authority_fixture(root, in_place=True)
    parent_batch = query_plan.build_statcast_root_batch(root)
    parent = next(
        query
        for query in parent_batch.queries
        if (
            date.fromisoformat(str(query.requested_range["end_date"]))
            - date.fromisoformat(str(query.requested_range["start_date"]))
        ).days
        >= 6
    )
    child_batch = query_plan.build_statcast_split_batch(
        root, parent_batch, parent
    )
    query_plan.publish_query_batch(root, parent_batch)
    query_plan.publish_query_batch(root, child_batch)
    attempt_number = _record_split_attempt(root, parent, "TIMEOUT")
    return parent_batch, parent, child_batch, attempt_number


class V52QueryPlanTest(unittest.TestCase):
    def test_source_authority_rejects_dirty_ephemeral_equivalence_addendum(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            (root / SOURCE_ADDENDUM).write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "addendum.*hash|binding"):
                query_plan._load_static_contracts(
                    root / SOURCE_RUN_SPEC.relative_to(ROOT),
                    root / SOURCE_CONTRACT.relative_to(ROOT),
                )

    def test_source_authority_rejects_wrong_addendum_git_blob(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            contract_path = root / SOURCE_CONTRACT.relative_to(ROOT)
            contract = json.loads(contract_path.read_bytes())
            contract["bindings"][
                "ephemeral_parse_schedule_equivalence_addendum"
            ]["git_blob_id"] = "0" * 40
            contract_path.write_bytes(_canonical_json_bytes(contract))
            with self.assertRaisesRegex(ValueError, "addendum.*Git blob"):
                query_plan._load_static_contracts(
                    root / SOURCE_RUN_SPEC.relative_to(ROOT), contract_path
                )

    def test_source_authority_rechecks_addendum_after_git_blob_verification(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            original = query_plan._verify_head_blob

            def replace_after_blob(
                repository_root, relative, contents, label, **kwargs
            ):
                result = original(
                    repository_root, relative, contents, label, **kwargs
                )
                if relative == SOURCE_ADDENDUM:
                    (root / SOURCE_ADDENDUM).write_text(
                        "tampered\\n", encoding="utf-8"
                    )
                return result

            with (
                mock.patch.object(
                    query_plan, "_verify_head_blob", side_effect=replace_after_blob
                ),
                self.assertRaisesRegex(ValueError, "addendum.*hash|binding"),
            ):
                query_plan._load_static_contracts(
                    root / SOURCE_RUN_SPEC.relative_to(ROOT),
                    root / SOURCE_CONTRACT.relative_to(ROOT),
                )

    def test_static_contract_context_selects_exact_old_and_source_authorities(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            old = query_plan._load_static_contracts(
                root / RUN_SPEC.relative_to(ROOT),
                root / CONTRACT.relative_to(ROOT),
            )
            source = query_plan._load_static_contracts(
                root / SOURCE_RUN_SPEC.relative_to(ROOT),
                root / SOURCE_CONTRACT.relative_to(ROOT),
            )

        self.assertIsInstance(old.authority, RunSpec)
        self.assertIsInstance(source.authority, SourceRunSpec)
        self.assertEqual(old.run_id, "historical_walkforward_500")
        self.assertEqual(
            source.run_id, "historical_walkforward_500_superseding_565534"
        )
        self.assertEqual(old.raw_root, root / "data/v5_2_qd/historical/raw")
        self.assertEqual(
            source.raw_root,
            root / "data/v5_2_qd/historical_superseding_565534/raw",
        )
        self.assertEqual(
            source.sources_root,
            root / "data/v5_2_qd/historical_superseding_565534/sources",
        )
        self.assertEqual(
            source.acquisition_audit_root,
            root / "audit/v5_2_qd/historical_superseding_565534/acquisition",
        )
        self.assertEqual(
            source.execution_audit_root,
            root / "audit/v5_2_qd/historical_superseding_565534/execution",
        )
        self.assertEqual(
            source.report_root,
            root / "reports/v5_2_qd/historical_superseding_565534",
        )

    def test_static_contract_context_rejects_old_source_cross_pairs(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            pairs = (
                (
                    root / RUN_SPEC.relative_to(ROOT),
                    root / SOURCE_CONTRACT.relative_to(ROOT),
                ),
                (
                    root / SOURCE_RUN_SPEC.relative_to(ROOT),
                    root / CONTRACT.relative_to(ROOT),
                ),
            )
            for run_spec_path, contract_path in pairs:
                with self.subTest(run_spec=run_spec_path.name), self.assertRaisesRegex(
                    ValueError, "pair|path|contract"
                ):
                    query_plan._load_static_contracts(run_spec_path, contract_path)

    def test_static_contract_context_rejects_path_alias_and_escape_attacks(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = _statcast_authority_fixture(base)
            source_run = root / SOURCE_RUN_SPEC.relative_to(ROOT)
            source_contract = root / SOURCE_CONTRACT.relative_to(ROOT)
            attacks = (
                root
                / "protocols/v5_2_qd/../v5_2_qd/"
                / SOURCE_RUN_SPEC.name,
                Path(str(source_run).replace("/protocols/", "\\protocols\\", 1)),
                base / "outside/historical_walkforward_500_superseding_565534.json",
            )
            for attack in attacks:
                with self.subTest(attack=attack), self.assertRaises(ValueError):
                    query_plan._load_static_contracts(attack, source_contract)

            saved = source_run.with_suffix(".saved")
            source_run.rename(saved)
            source_run.symlink_to(saved)
            with self.assertRaisesRegex(ValueError, "no-follow|regular|alias"):
                query_plan._load_static_contracts(source_run, source_contract)
            source_run.unlink()
            os.link(saved, source_run)
            with self.assertRaisesRegex(ValueError, "unalias|alias|regular"):
                query_plan._load_static_contracts(source_run, source_contract)

    def test_static_contract_context_rejects_namespace_symlink_and_nondirectory(self):
        import mlb_backtest.v52_query_plan as query_plan

        for attack in ("symlink", "file"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = _statcast_authority_fixture(base)
                data = root / "data"
                if attack == "symlink":
                    outside = base / "outside"
                    outside.mkdir()
                    data.symlink_to(outside, target_is_directory=True)
                else:
                    data.write_text("not a directory\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "namespace|directory|no-follow"):
                    query_plan._load_static_contracts(
                        root / SOURCE_RUN_SPEC.relative_to(ROOT),
                        root / SOURCE_CONTRACT.relative_to(ROOT),
                    )

    def test_2019_schedule_source_is_closed_derived_branch_with_null_new_raw_sha(self):
        """The superseding static plan replaces only 2019 with the closed disposition query."""
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            DISPOSITION_ENDPOINT,
            OLD_QUERY_ID,
            build_duplicate_disposition_authority,
        )
        from mlb_backtest.v52_query_plan import build_static_batch

        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=SOURCE_RUN_SPEC,
            acquisition_contract_path=SOURCE_CONTRACT,
        )
        batch = build_static_batch(SOURCE_RUN_SPEC, SOURCE_CONTRACT)

        self.assertEqual(batch.stage, "STATIC_SCHEDULES")
        self.assertIsNone(batch.derived_from_sha256)
        self.assertEqual(len(batch.queries), 7)
        self.assertEqual(
            [query.requested_range["year"] for query in batch.queries],
            list(range(2018, 2025)),
        )
        derived = batch.queries[1]
        self.assertEqual(derived, authority.query)
        self.assertEqual(derived.endpoint, DISPOSITION_ENDPOINT)
        self.assertEqual(dict(derived.parameters), {})
        self.assertIsNone(derived.parent_query_id)
        query_ids = [logical_query_id(query) for query in batch.queries]
        self.assertNotIn(OLD_QUERY_ID, query_ids)
        self.assertEqual(query_ids[1], logical_query_id(authority.query))
        self.assertEqual(
            [query.source for query in batch.queries],
            ["mlb_schedule_game_feed"] * 7,
        )

    def test_source_batch_publication_uses_authenticated_namespace_without_fallback(self):
        import mlb_backtest.v52_query_plan as query_plan
        from tests.test_v52_schedule_inventory import _source_fixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = _source_fixture(Path(directory))
            root = fixture.root
            batch = query_plan.build_static_batch(
                root / SOURCE_RUN_SPEC.relative_to(ROOT), fixture.contract_path
            )
            path = query_plan.publish_query_batch(root, batch)
            self.assertIn(
                "audit/v5_2_qd/historical_superseding_565534/acquisition/query_batches",
                path.as_posix(),
            )
            self.assertFalse(
                (root / "audit/v5_2_qd/historical/acquisition/query_batches" / f"{batch.batch_id}.json").exists()
            )
            self.assertEqual(query_plan.load_query_batch(root, batch.batch_id), batch)

    def test_source_reference_batch_generic_loader_is_deterministic_and_shadow_free(self):
        """Resume-style generic loads must select the source namespace by authority."""
        import mlb_backtest.execution_registry as execution_registry
        import mlb_backtest.v52_query_plan as query_plan
        from mlb_backtest.execution_registry import (
            load_latest_acquisition_checkpoint,
            publish_acquisition_checkpoint,
        )
        from mlb_backtest.v52_query_plan import (
            build_schedule_reference_batch,
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )
        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from tests.test_v52_schedule_inventory import (
            _memoized_disposition_projection_patch,
            _memoized_registry_git_patch,
            _source_registry_fixture,
        )

        with (
            _memoized_registry_git_patch(),
            _memoized_disposition_projection_patch(),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = _source_registry_fixture(Path(directory))
            static = build_static_batch(
                fixture.root / SOURCE_RUN_SPEC.relative_to(ROOT),
                fixture.contract_path,
            )
            disposition_query_id = logical_query_id(static.queries[1])
            completed_query_ids = tuple(
                sorted(logical_query_id(query) for query in static.queries)
            )
            static_checkpoint = publish_acquisition_checkpoint(
                fixture.root,
                fixture.run_spec,
                expected_prior_checkpoint_sha256=None,
                stage="STATIC_SCHEDULES",
                query_batch_ids=(static.batch_id,),
                completed_query_ids=completed_query_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                dispositioned_query_ids=(disposition_query_id,),
            )
            verified = publish_schedule_anchor_inventory(
                fixture.root,
                fixture.run_spec,
                acquisition_contract_path=fixture.contract_path,
                static_batch_id=static.batch_id,
                evidence_root=fixture.evidence_root,
            )
            reference = build_schedule_reference_batch(
                fixture.root,
                fixture.root / SOURCE_RUN_SPEC.relative_to(ROOT),
                acquisition_contract_path=fixture.contract_path,
                static_batch_id=static.batch_id,
                evidence_root=fixture.evidence_root,
            )
            self.assertEqual(reference.derived_from_sha256, verified.sha256)
            publish_query_batch(fixture.root, reference)
            reference_ids = tuple(
                sorted(logical_query_id(query) for query in reference.queries)
            )
            reference_checkpoint = publish_acquisition_checkpoint(
                fixture.root,
                fixture.run_spec,
                expected_prior_checkpoint_sha256=static_checkpoint.receipt_sha256,
                stage="REFERENCE_INPUTS",
                query_batch_ids=(static.batch_id, reference.batch_id),
                completed_query_ids=tuple(
                    sorted(logical_query_id(query) for query in static.queries)
                ),
                pending_query_ids=reference_ids,
                quarantined_query_ids=(),
                split_query_ids=(),
                dispositioned_query_ids=(disposition_query_id,),
                stage_evidence_receipts=(verified.path,),
            )
            self.assertEqual(
                load_latest_acquisition_checkpoint(
                    fixture.root, fixture.run_spec
                ),
                reference_checkpoint,
            )

            acquiring_path = next(
                path
                for path in (
                    fixture.root
                    / "audit/v5_2_qd/historical_superseding_565534/execution/phase_receipts"
                ).iterdir()
                if path.name.startswith("000001-ACQUIRING-")
            )
            forged_sources_ready = json.loads(acquiring_path.read_bytes())
            forged_sources_ready.update(
                {
                    "sequence": 2,
                    "phase": "SOURCES_READY",
                    "prior_receipt_sha256": hashlib.sha256(
                        acquiring_path.read_bytes()
                    ).hexdigest(),
                    "acquisition_checkpoint_head": {
                        "count": reference_checkpoint.sequence + 1,
                        "path": (
                            "audit/v5_2_qd/historical_superseding_565534/"
                            "execution/acquisition_checkpoints/"
                            f"{reference_checkpoint.sequence:08d}-"
                            f"{reference_checkpoint.receipt_sha256}.json"
                        ),
                        "sequence": reference_checkpoint.sequence,
                        "sha256": reference_checkpoint.receipt_sha256,
                        "stage": "REFERENCE_INPUTS",
                    },
                    "inventory": {
                        "path": verified.path.relative_to(fixture.root).as_posix(),
                        "sha256": verified.sha256,
                    },
                    "validation_receipts": [],
                }
            )
            with self.assertRaisesRegex(ValueError, "checkpoint|REMAINING|receipt"):
                execution_registry._validate_success_receipt(
                    forged_sources_ready, fixture.run_spec
                )

            reference_checkpoint_path = (
                fixture.root
                / "audit/v5_2_qd/historical_superseding_565534/execution/"
                / "acquisition_checkpoints"
                / (
                    f"{reference_checkpoint.sequence:08d}-"
                    f"{reference_checkpoint.receipt_sha256}.json"
                )
            )
            inventory_bytes = verified.path.read_bytes()
            checkpoint_bytes = reference_checkpoint_path.read_bytes()
            for mutation in ("inventory", "checkpoint"):
                with self.subTest(final_rebind=mutation):
                    fired = False

                    def mutate_final_authority() -> None:
                        nonlocal fired
                        if fired:
                            return
                        fired = True
                        target = (
                            verified.path
                            if mutation == "inventory"
                            else reference_checkpoint_path
                        )
                        target.write_bytes(target.read_bytes() + b" ")

                    with (
                        mock.patch.object(
                            query_plan,
                            "_before_query_batch_authority_rebind",
                            side_effect=mutate_final_authority,
                        ),
                        self.assertRaises((OSError, RuntimeError, ValueError)),
                    ):
                        load_latest_acquisition_checkpoint(
                            fixture.root, fixture.run_spec
                        )
                    verified.path.write_bytes(inventory_bytes)
                    reference_checkpoint_path.write_bytes(checkpoint_bytes)

            # This is the exact generic/public call shape used by resume and
            # registry validation paths; no authority argument is supplied.
            self.assertEqual(
                load_query_batch(fixture.root, reference.batch_id), reference
            )
            self.assertEqual(
                load_query_batch(
                    fixture.root, reference.batch_id, read_only_progress=True
                ),
                reference,
            )

            source_path = (
                fixture.root
                / "audit/v5_2_qd/historical_superseding_565534/acquisition/query_batches"
                / f"{reference.batch_id}.json"
            )
            old_path = (
                fixture.root
                / "audit/v5_2_qd/historical/acquisition/query_batches"
                / f"{reference.batch_id}.json"
            )
            old_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, old_path)
            with self.assertRaisesRegex(ValueError, "shadow|namespace|authority"):
                load_query_batch(fixture.root, reference.batch_id)

            source_path.unlink()
            with self.assertRaisesRegex(ValueError, "missing|source|authority"):
                load_query_batch(fixture.root, reference.batch_id)

    def test_source_authority_failure_never_writes_old_batch_namespace(self):
        import mlb_backtest.v52_query_plan as query_plan
        from tests.test_v52_schedule_inventory import _source_fixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = _source_fixture(Path(directory))
            root = fixture.root
            batch = query_plan.build_static_batch(
                root / SOURCE_RUN_SPEC.relative_to(ROOT), fixture.contract_path
            )
            forged_query = dataclasses.replace(
                batch.queries[0], run_spec_sha256="0" * 64
            )
            batch = dataclasses.replace(batch, queries=(forged_query, *batch.queries[1:]))
            with self.assertRaises(ValueError):
                query_plan.publish_query_batch(root, batch)
            self.assertFalse(
                (root / "audit/v5_2_qd/historical/acquisition/query_batches").exists()
            )

    def test_old_static_validator_rejects_derived_2019_slot(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        derived = batch.queries[1]
        forged = dataclasses.replace(
            derived,
            endpoint="disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            parameters={},
        )
        queries = (*batch.queries[:1], forged, *batch.queries[2:])
        with self.assertRaisesRegex(ValueError, "STATIC_SCHEDULES|canonical|schedule"):
            query_plan._validate_static_schedule_stage(None, queries)

    def test_suppressed_raw_object_sha_cannot_attach_to_another_anchor_or_edges(self):
        from mlb_backtest.v52_duplicate_gamepk_disposition import SUPPRESSED_OBJECT_SHA256
        from mlb_backtest.v52_query_plan import _build_schedule_reference_batch_from_inventory

        inventory, inventory_sha256 = _verified_schedule_inventory(
            [{"season": 2024, "game_pk": 565534, "played": True}]
        )
        inventory["anchors"][0]["raw_game_object_sha256"] = SUPPRESSED_OBJECT_SHA256
        with self.assertRaisesRegex(ValueError, "raw|anchor|565534"):
            _build_schedule_reference_batch_from_inventory(
                inventory,
                inventory_sha256=hashlib.sha256(
                    _canonical_json_bytes(inventory)
                ).hexdigest(),
                run_spec_sha256=RUN_SHA,
                acquisition_contract_sha256=CONTRACT_SHA,
            )

    def test_statcast_root_builder_exposes_private_transform_and_public_authority_api(
        self,
    ):
        import mlb_backtest.v52_query_plan as query_plan

        private = getattr(query_plan, "_build_statcast_root_batch", None)
        self.assertIsNotNone(
            private,
            "missing pure _build_statcast_root_batch(path, path) seam",
        )
        self.assertEqual(
            tuple(inspect.signature(private).parameters),
            ("run_spec_path", "acquisition_contract_path"),
        )
        self.assertEqual(
            tuple(inspect.signature(query_plan.build_statcast_root_batch).parameters),
            ("repository_root",),
        )

    def test_static_batch_covers_2018_through_2024_only_and_excludes_2025(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(len(batch.queries), 7)
        self.assertEqual(
            {query.source for query in batch.queries},
            {"mlb_schedule_game_feed"},
        )
        years = {
            int(str(query.requested_range["start_date"])[:4]) for query in batch.queries
        }
        self.assertEqual(years, set(range(2018, 2025)))
        self.assertFalse(
            any("2025" in canonical_query_bytes(q).decode() for q in batch.queries)
        )
        self.assertEqual(batch.stage, "STATIC_SCHEDULES")

    def test_statcast_root_batch_uses_monthly_official_parents_only(self):
        from mlb_backtest.v52_query_plan import _build_statcast_root_batch

        batch = _build_statcast_root_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(batch.stage, "REMAINING")
        self.assertIsNone(batch.derived_from_sha256)
        self.assertEqual(len(batch.queries), 46)
        self.assertEqual(
            {query.source for query in batch.queries},
            {"baseball_savant_statcast"},
        )
        self.assertEqual(
            {query.endpoint for query in batch.queries},
            {"https://baseballsavant.mlb.com/statcast_search/csv"},
        )
        for query in batch.queries:
            first = date.fromisoformat(str(query.requested_range["start_date"]))
            last = date.fromisoformat(str(query.requested_range["end_date"]))
            self.assertEqual(first.month, last.month)
            self.assertIsNone(query.parent_query_id)
            self.assertEqual(query.parameters["hfGT"], "R|")

    def test_static_and_statcast_root_batches_are_a_lossless_deterministic_split(self):
        from mlb_backtest.v52_query_plan import (
            _build_statcast_root_batch,
            build_static_batch,
        )

        static = build_static_batch(RUN_SPEC, CONTRACT)
        statcast = _build_statcast_root_batch(RUN_SPEC, CONTRACT)
        self.assertNotEqual(static.batch_id, statcast.batch_id)
        self.assertEqual(static, build_static_batch(RUN_SPEC, CONTRACT))
        self.assertEqual(
            statcast, _build_statcast_root_batch(RUN_SPEC, CONTRACT)
        )
        self.assertEqual(static.stage, "STATIC_SCHEDULES")
        self.assertEqual(statcast.stage, "REMAINING")

        static_ids = {logical_query_id(query) for query in static.queries}
        statcast_ids = {logical_query_id(query) for query in statcast.queries}
        self.assertTrue(static_ids.isdisjoint(statcast_ids))
        combined_ids = sorted(static_ids | statcast_ids)
        self.assertEqual(len(combined_ids), 53)
        population_digest = hashlib.sha256(
            ("\n".join(combined_ids) + "\n").encode()
        ).hexdigest()
        self.assertEqual(
            population_digest,
            "80f1272b36cbd906e1ca4b8d9f6d3b473c8633a9b1de2990816e56f62ee4eb4d",
        )
        for query in (*static.queries, *statcast.queries):
            self.assertEqual(query.run_spec_sha256, RUN_SHA)
            self.assertEqual(query.acquisition_contract_sha256, CONTRACT_SHA)
            reconstructed = LogicalQuery(**json.loads(canonical_query_bytes(query)))
            self.assertEqual(reconstructed, query)

    def test_root_batches_publish_and_load_independently(self):
        from mlb_backtest.v52_query_plan import (
            build_statcast_root_batch,
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        with tempfile.TemporaryDirectory() as td:
            root = _statcast_authority_fixture(Path(td))
            batches = (
                build_static_batch(RUN_SPEC, CONTRACT),
                build_statcast_root_batch(root),
            )
            paths = [publish_query_batch(root, batch) for batch in batches]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(
                [load_query_batch(root, batch.batch_id) for batch in batches],
                list(batches),
            )

    def test_statcast_public_builder_publishes_loads_and_is_idempotent(self):
        from mlb_backtest.v52_query_plan import (
            _build_statcast_root_batch,
            build_statcast_root_batch,
            load_query_batch,
            publish_query_batch,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            batch = build_statcast_root_batch(root)
            expected = _build_statcast_root_batch(
                root / "protocols/v5_2_qd/historical_walkforward_500.json",
                root / "protocols/v5_2_qd/historical_source_acquisition.json",
            )
            self.assertEqual(batch, expected)
            self.assertEqual(len(batch.queries), 46)
            path = publish_query_batch(root, batch)
            before = path.stat()
            self.assertEqual(publish_query_batch(root, batch), path)
            after = path.stat()
            self.assertEqual(
                (before.st_dev, before.st_ino),
                (after.st_dev, after.st_ino),
            )
            self.assertEqual(load_query_batch(root, batch.batch_id), batch)

    def test_statcast_root_publish_rebinds_authority_for_existing_and_new_files(
        self,
    ):
        import mlb_backtest.v52_query_plan as query_plan

        for publication in ("existing", "new"):
            with (
                self.subTest(publication=publication),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = _statcast_authority_fixture(Path(directory))
                batch = query_plan.build_statcast_root_batch(root)
                final = (
                    root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{batch.batch_id}.json"
                )
                expected: bytes | None = None
                if publication == "existing":
                    query_plan.publish_query_batch(root, batch)
                    expected = final.read_bytes()

                def mutate_authority() -> None:
                    contract = root / (
                        "protocols/v5_2_qd/"
                        "historical_source_acquisition.json"
                    )
                    contract.write_bytes(contract.read_bytes() + b" ")

                with (
                    mock.patch.object(
                        query_plan,
                        "_before_query_batch_authority_rebind",
                        side_effect=mutate_authority,
                    ) as hook,
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    query_plan.publish_query_batch(root, batch)
                hook.assert_called_once_with()
                if publication == "existing":
                    self.assertEqual(final.read_bytes(), expected)
                else:
                    self.assertFalse(final.exists())

    def test_statcast_root_load_rebinds_authority_after_reconstruction(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            batch = query_plan.build_statcast_root_batch(root)
            final = query_plan.publish_query_batch(root, batch)
            expected = final.read_bytes()

            def mutate_authority() -> None:
                contract = root / (
                    "protocols/v5_2_qd/historical_source_acquisition.json"
                )
                contract.write_bytes(contract.read_bytes() + b" ")

            with (
                mock.patch.object(
                    query_plan,
                    "_before_query_batch_authority_rebind",
                    side_effect=mutate_authority,
                ) as hook,
                self.assertRaises((ValueError, RuntimeError)),
            ):
                query_plan.load_query_batch(root, batch.batch_id)
            hook.assert_called_once_with()
            self.assertEqual(final.read_bytes(), expected)

    def test_query_batch_construction_rejects_unknown_stage_with_any_population(self):
        from mlb_backtest.v52_query_plan import _make_batch, build_static_batch

        query = build_static_batch(RUN_SPEC, CONTRACT).queries[0]
        for queries in ((), (query,)):
            with self.subTest(query_count=len(queries)), self.assertRaisesRegex(
                ValueError, "stage"
            ):
                _make_batch("TYPO_STAGE", None, queries)

    def test_load_recomputation_rejects_unknown_stage_with_any_population(self):
        from mlb_backtest.v52_query_plan import build_static_batch, load_query_batch

        query = build_static_batch(RUN_SPEC, CONTRACT).queries[0]
        for queries in ((), (query,)):
            with (
                self.subTest(query_count=len(queries)),
                tempfile.TemporaryDirectory() as td,
            ):
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="TYPO_STAGE",
                    derived_from_sha256=None,
                    queries=queries,
                )
                with self.assertRaisesRegex(ValueError, "stage"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_semantically_forged_static_root_batches(self):
        from mlb_backtest.v52_query_plan import build_static_batch, load_query_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        queries = batch.queries
        attacks = {
            "derived": ("c" * 64, queries),
            "order": (None, (queries[1], queries[0], *queries[2:])),
            "parent": (
                None,
                (dataclasses.replace(queries[0], parent_query_id="d" * 64), *queries[1:]),
            ),
            "endpoint": (
                None,
                (dataclasses.replace(queries[0], endpoint=queries[0].endpoint + "/x"), *queries[1:]),
            ),
            "grain": (
                None,
                (dataclasses.replace(queries[0], grain="pitch"), *queries[1:]),
            ),
            "media": (
                None,
                (dataclasses.replace(queries[0], expected_media_type="text/csv"), *queries[1:]),
            ),
            "parser": (
                None,
                (dataclasses.replace(queries[0], parser_version="v0"), *queries[1:]),
            ),
            "parameters": (
                None,
                (dataclasses.replace(queries[0], parameters={**queries[0].parameters, "extra": True}), *queries[1:]),
            ),
            "range": (
                None,
                (dataclasses.replace(queries[0], requested_range={**queries[0].requested_range, "extra": True}), *queries[1:]),
            ),
            "binding": (
                None,
                (dataclasses.replace(queries[0], run_spec_sha256="e" * 64), *queries[1:]),
            ),
        }
        for attack, (derived, forged_queries) in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="STATIC_SCHEDULES",
                    derived_from_sha256=derived,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(ValueError, "STATIC_SCHEDULES|static"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_semantically_forged_statcast_root_batches(self):
        from mlb_backtest.v52_query_plan import (
            _build_statcast_root_batch,
            load_query_batch,
        )

        queries = _build_statcast_root_batch(RUN_SPEC, CONTRACT).queries
        attacks = {
            "empty": (),
            "omission": queries[:-1],
            "extra": (*queries, queries[-1]),
            "order": (queries[1], queries[0], *queries[2:]),
            "parent": (
                dataclasses.replace(queries[0], parent_query_id="d" * 64),
                *queries[1:],
            ),
            "parameters": (
                dataclasses.replace(
                    queries[0], parameters={**queries[0].parameters, "hfGT": "P|"}
                ),
                *queries[1:],
            ),
            "range": (
                dataclasses.replace(
                    queries[0],
                    requested_range={**queries[0].requested_range, "end_date": "2018-04-01"},
                    parameters={**queries[0].parameters, "game_date_lt": "2018-04-01"},
                ),
                *queries[1:],
            ),
            "run-hash": tuple(
                dataclasses.replace(query, run_spec_sha256="e" * 64)
                for query in queries
            ),
            "contract-hash": tuple(
                dataclasses.replace(
                    query, acquisition_contract_sha256="e" * 64
                )
                for query in queries
            ),
        }
        for attack, forged_queries in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = _statcast_authority_fixture(Path(td))
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="REMAINING",
                    derived_from_sha256=None,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "REMAINING|Statcast|root|RunSpec|acquisition|authority|replay",
                ):
                    load_query_batch(root, batch_id)

    def test_remaining_derived_batches_require_valid_parented_statcast_queries(self):
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            _build_statcast_root_batch,
            bisect_statcast_query,
            load_query_batch,
        )

        root_query = _build_statcast_root_batch(RUN_SPEC, CONTRACT).queries[0]
        left, right = bisect_statcast_query(root_query)
        left_left, left_right = bisect_statcast_query(left)
        valid = _make_batch("REMAINING", "c" * 64, [left_left, left_right, right])
        self.assertTrue(all(query.parent_query_id is not None for query in valid.queries))

        attacks = {
            "empty": (),
            "missing_parent": (root_query,),
            "parameters": (
                dataclasses.replace(
                    left, parameters={**left.parameters, "hfGT": "P|"}
                ),
            ),
            "binding": (
                left,
                dataclasses.replace(right, run_spec_sha256="e" * 64),
            ),
        }
        for attack, forged_queries in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="REMAINING",
                    derived_from_sha256="f" * 64,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(ValueError, "REMAINING|Statcast|parent"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_static_remaining_source_mixing(self):
        from mlb_backtest.v52_query_plan import (
            _build_statcast_root_batch,
            build_static_batch,
            load_query_batch,
        )

        static = build_static_batch(RUN_SPEC, CONTRACT)
        statcast = _build_statcast_root_batch(RUN_SPEC, CONTRACT)
        attacks = (
            ("REMAINING", static.queries),
            ("STATIC_SCHEDULES", (*static.queries[:-1], statcast.queries[0])),
        )
        for stage, queries in attacks:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as td:
                payload = {
                    "stage": stage,
                    "derived_from_sha256": None,
                    "queries": [json.loads(canonical_query_bytes(q)) for q in queries],
                }
                batch_id = hashlib.sha256(
                    _canonical_json_bytes(payload).removesuffix(b"\n")
                ).hexdigest()
                record = {"schema_version": 1, "batch_id": batch_id, **payload}
                root = Path(td).resolve()
                batch_dir = (
                    root / "audit/v5_2_qd/historical/acquisition/query_batches"
                )
                batch_dir.mkdir(parents=True)
                (batch_dir / f"{batch_id}.json").write_bytes(
                    _canonical_json_bytes(record)
                )
                with self.assertRaisesRegex(
                    ValueError, "stage|source|STATIC_SCHEDULES|Statcast"
                ):
                    load_query_batch(root, batch_id)

    def test_static_batch_rehashes_the_bound_plan_a_amendment(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            run_spec = root / RUN_SPEC.relative_to(ROOT)
            forged_contract = root / CONTRACT.relative_to(ROOT)
            contract = json.loads(forged_contract.read_bytes())
            contract["bindings"]["plan_a_amendment"]["sha256"] = "0" * 64
            forged_contract.write_bytes(_canonical_json_bytes(contract))
            with self.assertRaisesRegex(ValueError, "amendment.*hash|binding"):
                build_static_batch(run_spec, forged_contract)

    def test_static_batch_rehashes_the_bound_design_and_plan(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        cases = (
            ("approved_design", "sha256", "0" * 64, "approved design|binding"),
            (
                "implementation_plan",
                "working_tree_sha256",
                "0" * 64,
                "implementation plan|binding",
            ),
            (
                "implementation_plan",
                "git_blob_id",
                "0" * 40,
                "implementation plan|Git|binding",
            ),
        )
        for binding, key, forged, error in cases:
            with tempfile.TemporaryDirectory() as directory:
                with self.subTest(binding=binding, key=key):
                    root = _statcast_authority_fixture(Path(directory))
                    run_spec = root / RUN_SPEC.relative_to(ROOT)
                    forged_contract = root / CONTRACT.relative_to(ROOT)
                    contract = json.loads(forged_contract.read_bytes())
                    contract["bindings"][binding][key] = forged
                    forged_contract.write_bytes(_canonical_json_bytes(contract))
                    with self.assertRaisesRegex(ValueError, error):
                        build_static_batch(run_spec, forged_contract)

    def test_static_batch_rejects_noncanonical_acquisition_contract(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        pretty = (
            json.dumps(
                json.loads(CONTRACT.read_bytes()),
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            run_spec = root / RUN_SPEC.relative_to(ROOT)
            noncanonical_contract = root / CONTRACT.relative_to(ROOT)
            noncanonical_contract.write_bytes(pretty)
            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                build_static_batch(run_spec, noncanonical_contract)

    def test_static_batch_requires_the_frozen_acquisition_contract_schema(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        def mutate_statcast_limit(contract):
            contract["response_byte_limits"]["statcast"] = 1

        def mutate_timezone_path(contract):
            contract["timezone_mapping_path"] = "config/v5_2_qd_venue_timezones.json"

        def mutate_completeness_limit(contract):
            contract["response_completeness_limits"]["statcast_data_row_limit"] = 24_999

        def mutate_source_grain(contract):
            source = next(
                item
                for item in contract["sources"]
                if item["name"] == "mlb_stats_pitcher_bf"
            )
            source["grain"] = "forged_grain"

        def add_unknown_key(contract):
            contract["unexpected"] = True

        def mutate_schema_version_to_equal_float(contract):
            contract["schema_version"] = 2.0

        def mutate_split_limit_to_equal_float(contract):
            contract["response_completeness_limits"]["league_pitching_split_limit"] = (
                5000.0
            )

        cases = (
            ("statcast response cap", mutate_statcast_limit),
            ("timezone mapping path", mutate_timezone_path),
            ("response completeness", mutate_completeness_limit),
            ("source grain", mutate_source_grain),
            ("unknown top-level key", add_unknown_key),
            ("float schema version", mutate_schema_version_to_equal_float),
            ("float split limit", mutate_split_limit_to_equal_float),
        )
        for label, mutate in cases:
            with tempfile.TemporaryDirectory() as directory:
                with self.subTest(label=label):
                    root = _statcast_authority_fixture(Path(directory))
                    run_spec = root / RUN_SPEC.relative_to(ROOT)
                    forged_contract = root / CONTRACT.relative_to(ROOT)
                    contract = json.loads(forged_contract.read_bytes())
                    mutate(contract)
                    forged_contract.write_bytes(_canonical_json_bytes(contract))
                    with self.assertRaisesRegex(ValueError, "schema|frozen|contract"):
                        build_static_batch(run_spec, forged_contract)

    def test_static_batch_ignores_poisoned_git_repository_environment(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        poison = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(ROOT / "missing-objects"),
            "GIT_COMMON_DIR": str(ROOT / "missing-common-dir"),
            "GIT_DIR": str(ROOT / "missing-git-dir"),
            "GIT_INDEX_FILE": str(ROOT / "missing-index"),
            "GIT_OBJECT_DIRECTORY": str(ROOT / "missing-object-dir"),
            "GIT_WORK_TREE": str(ROOT / "missing-work-tree"),
        }
        with mock.patch.dict(os.environ, poison, clear=False):
            batch = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertTrue(batch.queries)

    def test_static_batch_rejects_an_uncommitted_matching_schema(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            run_spec = root / RUN_SPEC.relative_to(ROOT)
            forged_contract = root / CONTRACT.relative_to(ROOT)
            forged_schema = root / "schemas/v5_2_acquisition_contract.schema.json"
            contract = json.loads(forged_contract.read_bytes())
            contract["response_byte_limits"]["statcast"] = 1
            schema = json.loads(forged_schema.read_bytes())
            schema["oneOf"][0]["const"] = contract
            forged_contract.write_bytes(_canonical_json_bytes(contract))
            forged_schema.write_bytes(_canonical_json_bytes(schema))
            with self.assertRaisesRegex(ValueError, "schema.*Git|blob|committed"):
                build_static_batch(run_spec, forged_contract)

    def test_bisect_children_bind_parent_and_cover_without_gap_or_overlap(self):
        from mlb_backtest.v52_query_plan import bisect_statcast_query

        parent = _parent("2024-06-01", "2024-06-30")
        left, right = bisect_statcast_query(parent)
        self.assertEqual(left.parent_query_id, logical_query_id(parent))
        self.assertEqual(right.parent_query_id, logical_query_id(parent))
        self.assertEqual(
            left.requested_range,
            {"year": 2024, "start_date": "2024-06-01", "end_date": "2024-06-15"},
        )
        self.assertEqual(
            right.requested_range,
            {"year": 2024, "start_date": "2024-06-16", "end_date": "2024-06-30"},
        )
        self.assertEqual(left.parameters["game_date_lt"], "2024-06-15")
        self.assertEqual(right.parameters["game_date_gt"], "2024-06-16")
        with self.assertRaisesRegex(ValueError, "single-day"):
            bisect_statcast_query(_parent("2024-06-01", "2024-06-01"))

    def test_statcast_split_batch_builder_seals_the_canonical_bisection(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            parent_batch = query_plan.build_statcast_root_batch(root)
            parent = next(
                query
                for query in parent_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )

            first = query_plan.build_statcast_split_batch(
                root, parent_batch, parent
            )
            second = query_plan.build_statcast_split_batch(
                root, parent_batch, parent
            )

            self.assertEqual(first, second)
            self.assertEqual(first.stage, "REMAINING")
            self.assertEqual(first.derived_from_sha256, parent_batch.batch_id)
            self.assertEqual(
                first.queries,
                query_plan.bisect_statcast_query(parent),
            )
            self.assertEqual(
                first,
                query_plan._make_batch(
                    "REMAINING",
                    parent_batch.batch_id,
                    first.queries,
                ),
            )

    def test_statcast_split_batch_builder_rejects_unsealed_or_nonmember_inputs(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            parent_batch = query_plan.build_statcast_root_batch(root)
            parent = next(
                query
                for query in parent_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            unsealed = dataclasses.replace(parent_batch, batch_id="f" * 64)
            nonmember = dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "start_date": "2024-04-01",
                    "end_date": "2024-04-02",
                },
                parameters={
                    **parent.parameters,
                    "game_date_gt": "2024-04-01",
                    "game_date_lt": "2024-04-02",
                },
            )

            with self.assertRaisesRegex(ValueError, "batch_id|canonical|sealed"):
                query_plan.build_statcast_split_batch(root, unsealed, parent)
            with self.assertRaisesRegex(ValueError, "occur exactly once|member"):
                query_plan.build_statcast_split_batch(
                    root, parent_batch, nonmember
                )

            query_plan.publish_query_batch(root, parent_batch)
            canonical = query_plan.build_statcast_split_batch(
                root, parent_batch, parent
            )
            left, right = canonical.queries
            left_left, left_right = query_plan.bisect_statcast_query(left)
            parent_query_id = logical_query_id(parent)
            noncanonical_batch = query_plan._make_batch(
                "REMAINING",
                parent_batch.batch_id,
                (
                    dataclasses.replace(
                        left_left, parent_query_id=parent_query_id
                    ),
                    dataclasses.replace(
                        left_right, parent_query_id=parent_query_id
                    ),
                    right,
                ),
            )
            with self.assertRaisesRegex(ValueError, "canonical.*parent batch|bisection"):
                query_plan.build_statcast_split_batch(
                    root, noncanonical_batch, noncanonical_batch.queries[0]
                )

    def test_statcast_split_batch_builder_rejects_single_day_parent(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            parent_batch = query_plan.build_statcast_root_batch(root)
            parent = next(
                query
                for query in parent_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            query_plan.publish_query_batch(root, parent_batch)
            while (
                date.fromisoformat(str(parent.requested_range["start_date"]))
                != date.fromisoformat(str(parent.requested_range["end_date"]))
            ):
                parent_batch = query_plan.build_statcast_split_batch(
                    root, parent_batch, parent
                )
                query_plan.publish_query_batch(root, parent_batch)
                parent = parent_batch.queries[0]

            with self.assertRaisesRegex(ValueError, "single-day"):
                query_plan.build_statcast_split_batch(
                    root, parent_batch, parent
                )

    def test_schedule_reference_batch_projects_exact_boxscore_pitching_team_and_venue_queries(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )

        inventory, inventory_sha256 = _verified_schedule_inventory(
            [
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 7,
                    "official_date": "2024-04-02",
                    "venue_id": 31,
                    "played": True,
                },
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 8,
                    "official_date": "2024-04-02",
                    "venue_id": 31,
                    "played": False,
                },
            ]
        )
        batch = _build_schedule_reference_batch_from_inventory(
            inventory,
            inventory_sha256=inventory_sha256,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        sources = [query.source for query in batch.queries]
        self.assertEqual(sources.count("mlb_schedule_game_feed"), 1)
        boxscore = next(
            q for q in batch.queries if q.source == "mlb_schedule_game_feed"
        )
        self.assertEqual(boxscore.requested_range["game_pk"], 7)
        self.assertEqual(sources.count("mlb_stats_pitching"), 1)
        pitching = next(q for q in batch.queries if q.source == "mlb_stats_pitching")
        self.assertEqual(pitching.requested_range["consuming_game_pks"], [7])
        self.assertEqual(
            pitching.requested_range["consumer_cutoff_dates"],
            {"7": "2024-04-01"},
        )
        self.assertEqual(pitching.parameters["limit"], 5000)
        self.assertEqual(
            pitching.requested_range["response_completeness"],
            {"limit": 5000, "reject_total_splits_at_or_above_limit": True},
        )
        self.assertEqual(sum(q.endpoint.endswith("/teams") for q in batch.queries), 7)
        self.assertEqual(sum("/venues/31" in q.endpoint for q in batch.queries), 1)
        self.assertEqual(batch.stage, "REFERENCE_INPUTS")
        self.assertEqual(batch.derived_from_sha256, inventory_sha256)

    def test_schedule_reference_rejects_wrong_start_offseason_and_conflicting_season_rows(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )

        base = {
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 7,
            "official_date": "2024-04-02",
            "venue_id": 31,
            "played": True,
        }
        invalid_anchor_sets = [
            [{**base, "season_start_date": "2024-03-21"}],
            [{**base, "official_date": "2024-10-01"}],
            [
                    base,
                    {
                        **base,
                        "game_pk": 8,
                        "season_start_date": "2024-03-19",
                    },
            ],
        ]
        for anchors in invalid_anchor_sets:
            inventory, inventory_sha256 = _verified_schedule_inventory(anchors)
            with self.subTest(anchors=anchors), self.assertRaises(ValueError):
                _build_schedule_reference_batch_from_inventory(
                    inventory,
                    inventory_sha256=inventory_sha256,
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_opening_day_d1_emits_preseason_disposition_not_invalid_stats_query(self):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )

        inventory, inventory_sha256 = _verified_schedule_inventory(
            [
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 1,
                    "official_date": "2024-03-20",
                    "venue_id": 31,
                    "played": True,
                }
            ]
        )
        batch = _build_schedule_reference_batch_from_inventory(
            inventory,
            inventory_sha256=inventory_sha256,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        pitching = [q for q in batch.queries if q.source == "mlb_stats_pitching"]
        self.assertEqual(len(pitching), 1)
        self.assertEqual(
            pitching[0].endpoint,
            "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )
        self.assertEqual(
            pitching[0].requested_range["disposition"],
            "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )
        self.assertEqual(
            pitching[0].requested_range["reason"],
            "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
        )
        self.assertNotIn("startDate", pitching[0].parameters)

    def test_all_unplayed_schedule_population_emits_no_d1_query_or_disposition(self):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )

        inventory, inventory_sha256 = _verified_schedule_inventory()
        batch = _build_schedule_reference_batch_from_inventory(
            inventory,
            inventory_sha256=inventory_sha256,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertFalse(any(q.source == "mlb_stats_pitching" for q in batch.queries))

    def test_schedule_reference_requires_complete_inventory_and_rejects_any_duplicate(self):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )

        base = {
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 7,
            "official_date": "2024-04-02",
            "venue_id": 31,
        }
        missing, _ = _verified_schedule_inventory([{**base, "played": True}])
        del missing["anchors"][-1]["played"]
        duplicate, duplicate_sha = _verified_schedule_inventory(
            [{**base, "played": True}, {**base, "played": True}]
        )
        valid, _ = _verified_schedule_inventory(
            [{**base, "played": True}]
        )
        forged_run = json.loads(_canonical_json_bytes(valid))
        forged_run["run_spec"]["sha256"] = "9" * 64
        forged_contract = json.loads(_canonical_json_bytes(valid))
        forged_contract["acquisition_contract"]["sha256"] = "8" * 64
        attacks = (
            (missing, hashlib.sha256(_canonical_json_bytes(missing)).hexdigest()),
            (duplicate, duplicate_sha),
            ({"anchors": [_inventory_anchor()]}, "f" * 64),
            (valid, "0" * 64),
            (
                forged_run,
                hashlib.sha256(_canonical_json_bytes(forged_run)).hexdigest(),
            ),
            (
                forged_contract,
                hashlib.sha256(
                    _canonical_json_bytes(forged_contract)
                ).hexdigest(),
            ),
        )
        for inventory, inventory_sha256 in attacks:
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                _build_schedule_reference_batch_from_inventory(
                    inventory,
                    inventory_sha256=inventory_sha256,
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_schedule_reference_public_builder_loads_fixed_verified_inventory(self):
        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_schedule_reference_batch,
            load_query_batch,
            publish_query_batch,
        )
        from tests.test_v52_schedule_inventory import _fixture, _kwargs

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            verified = publish_schedule_anchor_inventory(**_kwargs(fixture))
            batch = build_schedule_reference_batch(
                fixture.root,
                fixture.root
                / "protocols/v5_2_qd/historical_walkforward_500.json",
                acquisition_contract_path=fixture.root
                / "protocols/v5_2_qd/historical_source_acquisition.json",
                static_batch_id=fixture.batch_id,
                evidence_root=fixture.evidence_root,
            )
            self.assertEqual(batch.derived_from_sha256, verified.sha256)
            self.assertEqual(batch.stage, "REFERENCE_INPUTS")
            publish_query_batch(fixture.root, batch)
            self.assertEqual(
                load_query_batch(fixture.root, batch.batch_id), batch
            )
            with self.assertRaises((TypeError, ValueError)):
                build_schedule_reference_batch(  # type: ignore[call-arg]
                    verified.payload,
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_reference_stage_validator_rejects_forged_structure_on_make_and_load(self):
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
            _make_batch,
            load_query_batch,
        )

        inventory, inventory_sha256 = _verified_schedule_inventory(
            [
                {
                    "season": 2024,
                    "game_pk": 7,
                    "official_date": "2024-04-02",
                    "played": True,
                    "venue_id": 31,
                },
                {
                    "season": 2024,
                    "game_pk": 8,
                    "official_date": "2024-04-02",
                    "played": True,
                    "venue_id": 32,
                },
            ]
        )
        valid = _build_schedule_reference_batch_from_inventory(
            inventory,
            inventory_sha256=inventory_sha256,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        boxscore_index = next(
            index
            for index, query in enumerate(valid.queries)
            if query.source == "mlb_schedule_game_feed"
        )
        team_index = next(
            index
            for index, query in enumerate(valid.queries)
            if query.endpoint.endswith("/teams")
        )
        forged_boxscore = dataclasses.replace(
            valid.queries[boxscore_index],
            endpoint=valid.queries[boxscore_index].endpoint + "/forged",
        )
        pitching_index = next(
            index
            for index, query in enumerate(valid.queries)
            if query.source == "mlb_stats_pitching"
        )
        pitching_query = valid.queries[pitching_index]
        split_pitching = tuple(
            dataclasses.replace(
                pitching_query,
                requested_range={
                    **pitching_query.requested_range,
                    "consumer_cutoff_dates": {
                        str(game_pk): pitching_query.requested_range[
                            "as_of_date"
                        ]
                    },
                    "consuming_game_pks": [game_pk],
                },
            )
            for game_pk in pitching_query.requested_range["consuming_game_pks"]
        )
        attacks = {
            "missing-derived": (None, valid.queries),
            "empty": (inventory_sha256, ()),
            "boxscore": (
                inventory_sha256,
                (*valid.queries[:boxscore_index], forged_boxscore, *valid.queries[boxscore_index + 1 :]),
            ),
            "missing-team": (
                inventory_sha256,
                (*valid.queries[:team_index], *valid.queries[team_index + 1 :]),
            ),
            "split-pitching-group": (
                inventory_sha256,
                (
                    *valid.queries[:pitching_index],
                    *split_pitching,
                    *valid.queries[pitching_index + 1 :],
                ),
            ),
        }
        with self.assertRaisesRegex(ValueError, "REFERENCE_INPUTS|boxscore"):
            _make_batch(
                "REFERENCE_INPUTS",
                inventory_sha256,
                attacks["boxscore"][1],
            )
        with self.assertRaisesRegex(ValueError, "REFERENCE_INPUTS|pitching"):
            _make_batch(
                "REFERENCE_INPUTS",
                inventory_sha256,
                attacks["split-pitching-group"][1],
            )
        for attack, (derived, queries) in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="REFERENCE_INPUTS",
                    derived_from_sha256=derived,
                    queries=tuple(queries),
                )
                with self.assertRaisesRegex(
                    ValueError, "REFERENCE_INPUTS|reference|derived"
                ):
                    load_query_batch(root, batch_id)

    def test_reference_publish_and_load_replay_fixed_inventory_exactly(self):
        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            build_schedule_reference_batch,
            load_query_batch,
            publish_query_batch,
        )
        from tests.test_v52_schedule_inventory import _fixture, _kwargs

        with tempfile.TemporaryDirectory() as td:
            fixture = _fixture(Path(td).resolve())
            verified = publish_schedule_anchor_inventory(**_kwargs(fixture))
            valid = build_schedule_reference_batch(
                fixture.root,
                fixture.root
                / "protocols/v5_2_qd/historical_walkforward_500.json",
                acquisition_contract_path=fixture.root
                / "protocols/v5_2_qd/historical_source_acquisition.json",
                static_batch_id=fixture.batch_id,
                evidence_root=fixture.evidence_root,
            )
            no_played = tuple(
                query
                for query in valid.queries
                if query.source
                not in {"mlb_schedule_game_feed", "mlb_stats_pitching"}
            )
            venue_indexes = [
                index
                for index, query in enumerate(valid.queries)
                if "/venues/" in query.endpoint
            ]
            venue_subset = tuple(
                query
                for index, query in enumerate(valid.queries)
                if index != venue_indexes[-1]
            )
            last_venue = valid.queries[venue_indexes[-1]]
            extra_venue = dataclasses.replace(
                last_venue,
                endpoint="https://statsapi.mlb.com/api/v1/venues/999",
                requested_range={"venue_id": 999},
            )
            venue_extra = (*valid.queries, extra_venue)
            semantic_attacks = {
                "played-subset": no_played,
                "venue-subset": venue_subset,
                "venue-extra": venue_extra,
            }
            for attack, queries in semantic_attacks.items():
                forged = _make_batch(
                    "REFERENCE_INPUTS", verified.sha256, queries
                )
                with self.subTest(attack=attack), self.assertRaisesRegex(
                    ValueError, "REFERENCE_INPUTS|inventory|reference|replay"
                ):
                    publish_query_batch(fixture.root, forged)
                forged_id = _write_forged_canonical_batch(
                    fixture.root,
                    stage="REFERENCE_INPUTS",
                    derived_from_sha256=verified.sha256,
                    queries=tuple(queries),
                )
                with self.assertRaisesRegex(
                    ValueError, "REFERENCE_INPUTS|inventory|reference|replay"
                ):
                    load_query_batch(fixture.root, forged_id)

            team_index = next(
                index
                for index, query in enumerate(valid.queries)
                if query.endpoint.endswith("/teams")
            )
            team_omission = (
                *valid.queries[:team_index],
                *valid.queries[team_index + 1 :],
            )
            forged_id = _write_forged_canonical_batch(
                fixture.root,
                stage="REFERENCE_INPUTS",
                derived_from_sha256=verified.sha256,
                queries=team_omission,
            )
            with self.assertRaises(ValueError):
                load_query_batch(fixture.root, forged_id)

    def test_reference_publish_rebinds_authority_before_existing_and_new_returns(self):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from tests.test_v52_schedule_inventory import _fixture, _kwargs

        for publication in ("existing", "new"):
            with self.subTest(publication=publication), tempfile.TemporaryDirectory() as td:
                fixture = _fixture(Path(td).resolve())
                verified = publish_schedule_anchor_inventory(**_kwargs(fixture))
                batch = query_plan.build_schedule_reference_batch(
                    fixture.root,
                    fixture.root
                    / "protocols/v5_2_qd/historical_walkforward_500.json",
                    acquisition_contract_path=fixture.root
                    / "protocols/v5_2_qd/historical_source_acquisition.json",
                    static_batch_id=fixture.batch_id,
                    evidence_root=fixture.evidence_root,
                )
                final = (
                    fixture.root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{batch.batch_id}.json"
                )
                if publication == "existing":
                    query_plan.publish_query_batch(fixture.root, batch)
                    expected = final.read_bytes()

                def mutate_authority() -> None:
                    payload = json.loads(verified.path.read_bytes())
                    payload["counts"]["game_count"] += 1
                    verified.path.write_bytes(_canonical_json_bytes(payload))

                with (
                    mock.patch.object(
                        query_plan,
                        "_before_query_batch_authority_rebind",
                        side_effect=mutate_authority,
                        create=True,
                    ) as hook,
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    query_plan.publish_query_batch(fixture.root, batch)
                hook.assert_called_once_with()
                if publication == "existing":
                    self.assertEqual(final.read_bytes(), expected)
                else:
                    self.assertFalse(final.exists())

    def test_participant_batch_uses_only_verified_actual_starters_and_deduplicates(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_participant_derived_batch_from_inventory,
        )

        inventory = {
            "games": [
                {
                    "game_pk": 1,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 2,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 300],
                },
                {
                    "game_pk": 3,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": False,
                    "verification_status": "UNVERIFIED",
                    "participant_provenance": "PROBABLE",
                    "actual_starter_ids": [999],
                },
            ]
        }
        batch = _build_participant_derived_batch_from_inventory(
            inventory,
            inventory_sha256=hashlib.sha256(
                _canonical_json_bytes(inventory)
            ).hexdigest(),
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(batch.stage, "PARTICIPANT_DERIVED")
        self.assertEqual(
            [q.requested_range["pitcher_id"] for q in batch.queries], [100, 200, 300]
        )
        self.assertTrue(
            all(
                q.endpoint
                == f"https://statsapi.mlb.com/api/v1/people/{q.requested_range['pitcher_id']}/stats"
                for q in batch.queries
            )
        )
        self.assertTrue(all("personId" not in q.parameters for q in batch.queries))
        self.assertTrue(
            all(q.parameters["endDate"] == "2021-12-31" for q in batch.queries)
        )
        by_pitcher = {q.requested_range["pitcher_id"]: q for q in batch.queries}
        self.assertEqual(by_pitcher[100].requested_range["consuming_game_pks"], [1, 2])
        self.assertEqual(
            by_pitcher[100].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01", "2": "2024-04-01"},
        )
        self.assertEqual(
            by_pitcher[200].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01"},
        )

    def test_participant_batch_splits_each_pitcher_by_cutoff_baseline_without_loss(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_participant_derived_batch_from_inventory,
        )

        inventory = {
            "games": [
                {
                    "game_pk": 11,
                    "season": 2018,
                    "official_date": "2018-03-29",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 22,
                    "season": 2022,
                    "official_date": "2022-04-07",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 300],
                },
            ]
        }
        batch = _build_participant_derived_batch_from_inventory(
            inventory,
            inventory_sha256=hashlib.sha256(
                _canonical_json_bytes(inventory)
            ).hexdigest(),
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        observed = [
            (
                query.requested_range["pitcher_id"],
                query.requested_range["inclusive_end"],
                query.requested_range["consuming_game_pks"],
                query.requested_range["consumer_cutoff_dates"],
                query.parameters["endDate"],
                query.endpoint,
            )
            for query in batch.queries
        ]
        self.assertEqual(
            observed,
            [
                (
                    100,
                    "2017-12-31",
                    [11],
                    {"11": "2018-03-28"},
                    "2017-12-31",
                    "https://statsapi.mlb.com/api/v1/people/100/stats",
                ),
                (
                    100,
                    "2021-12-31",
                    [22],
                    {"22": "2022-04-06"},
                    "2021-12-31",
                    "https://statsapi.mlb.com/api/v1/people/100/stats",
                ),
                (
                    200,
                    "2017-12-31",
                    [11],
                    {"11": "2018-03-28"},
                    "2017-12-31",
                    "https://statsapi.mlb.com/api/v1/people/200/stats",
                ),
                (
                    300,
                    "2021-12-31",
                    [22],
                    {"22": "2022-04-06"},
                    "2021-12-31",
                    "https://statsapi.mlb.com/api/v1/people/300/stats",
                ),
            ],
        )
        all_game_pks = [
            game_pk
            for query in batch.queries
            for game_pk in query.requested_range["consuming_game_pks"]
        ]
        self.assertEqual(sorted(all_game_pks), [11, 11, 22, 22])
        query_ids = [logical_query_id(query) for query in batch.queries]
        self.assertEqual(len(query_ids), len(set(query_ids)))
        consumers_by_pitcher: dict[int, list[int]] = {}
        for query in batch.queries:
            consumers_by_pitcher.setdefault(
                int(query.requested_range["pitcher_id"]), []
            ).extend(query.requested_range["consuming_game_pks"])
        self.assertEqual(
            {
                pitcher_id: sorted(game_pks)
                for pitcher_id, game_pks in consumers_by_pitcher.items()
            },
            {100: [11, 22], 200: [11], 300: [22]},
        )

    def test_participant_batch_validates_2018_2024_season_and_game_date_boundaries(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_participant_derived_batch_from_inventory,
        )

        valid = {
            "games": [
                {
                    "game_pk": 1,
                    "season": 2018,
                    "official_date": "2018-03-29",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 2,
                    "season": 2024,
                    "official_date": "2024-09-30",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [300, 400],
                },
            ]
        }
        batch = _build_participant_derived_batch_from_inventory(
            valid,
            inventory_sha256=hashlib.sha256(
                _canonical_json_bytes(valid)
            ).hexdigest(),
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(
            [query.requested_range["pitcher_id"] for query in batch.queries],
            [100, 200, 300, 400],
        )

        invalid_games = [
            {**valid["games"][0], "season": 2025, "official_date": "2025-03-27"},
            {**valid["games"][0], "season": 2017, "official_date": "2017-04-02"},
            {**valid["games"][0], "season": 2024, "official_date": "2023-04-02"},
            {**valid["games"][0], "season": 2024, "official_date": "not-a-date"},
        ]
        for game in invalid_games:
            with self.subTest(game=game), self.assertRaises(ValueError):
                inventory = {"games": [game]}
                _build_participant_derived_batch_from_inventory(
                    inventory,
                    inventory_sha256=hashlib.sha256(
                        _canonical_json_bytes(inventory)
                    ).hexdigest(),
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_participant_batch_rejects_duplicate_games_and_identical_starters(self):
        from mlb_backtest.v52_query_plan import (
            _build_participant_derived_batch_from_inventory,
        )

        base = {
            "game_pk": 1,
            "season": 2024,
            "official_date": "2024-04-02",
            "played": True,
            "verification_status": "VERIFIED",
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "actual_starter_ids": [100, 200],
        }
        invalid_games = [
            [base, dict(base)],
            [base, {**base, "actual_starter_ids": [300, 400]}],
            [{**base, "actual_starter_ids": [100, 100]}],
            [{key: value for key, value in base.items() if key != "played"}],
        ]
        for games in invalid_games:
            with self.subTest(games=games), self.assertRaises(ValueError):
                inventory = {"games": games}
                _build_participant_derived_batch_from_inventory(
                    inventory,
                    inventory_sha256=hashlib.sha256(
                        _canonical_json_bytes(inventory)
                    ).hexdigest(),
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_participant_public_builder_uses_fixed_verified_inventory_file_hash(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inventory = _participant_authority_fixture(root)
            contents = inventory.path.read_bytes()
            expected = hashlib.sha256(contents).hexdigest()
            self.assertTrue(contents.endswith(b"\n"))
            self.assertNotEqual(
                expected,
                hashlib.sha256(contents.removesuffix(b"\n")).hexdigest(),
            )
            batch = build_participant_derived_batch(root)
            self.assertEqual(batch.stage, "PARTICIPANT_DERIVED")
            self.assertEqual(batch.derived_from_sha256, expected)
            self.assertEqual(inventory.sha256, expected)
            self.assertTrue(batch.queries)

    def test_participant_public_builder_rejects_legacy_mapping_signature(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inventory = _participant_authority_fixture(root)
            payload = json.loads(inventory.path.read_bytes())
            with self.assertRaises(TypeError):
                build_participant_derived_batch(  # type: ignore[call-arg]
                    payload,
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_participant_stage_semantic_validator_rejects_closed_attacks(self):
        from mlb_backtest import v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _participant_authority_fixture(root)
            valid = query_plan.build_participant_derived_batch(root)
            first = valid.queries[0]
            first_consumer = str(first.requested_range["consuming_game_pks"][0])
            out_of_run_cutoffs = dict(
                first.requested_range["consumer_cutoff_dates"]
            )
            out_of_run_cutoffs[first_consumer] = "9999-01-01"
            attacks = {
                "closed-field": (
                    dataclasses.replace(
                        first,
                        parameters={**first.parameters, "unexpected": "field"},
                    ),
                    *valid.queries[1:],
                ),
                "order": tuple(reversed(valid.queries)),
                "baseline": (
                    dataclasses.replace(
                        first,
                        parameters={**first.parameters, "endDate": "2020-12-31"},
                    ),
                    *valid.queries[1:],
                ),
                "consumer-cutoff-out-of-run": (
                    dataclasses.replace(
                        first,
                        requested_range={
                            **first.requested_range,
                            "consumer_cutoff_dates": out_of_run_cutoffs,
                        },
                    ),
                    *valid.queries[1:],
                ),
            }
            for attack, queries in attacks.items():
                with self.subTest(attack=attack), self.assertRaises(ValueError):
                    query_plan._make_batch(
                        "PARTICIPANT_DERIVED",
                        valid.derived_from_sha256,
                        queries,
                    )

    def test_participant_fixed_replay_rejects_omitted_extra_and_wrong_derived(self):
        from mlb_backtest.v52_query_plan import (
            build_participant_derived_batch,
            load_query_batch,
            publish_query_batch,
        )

        for attack in ("omitted", "extra", "wrong-derived"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _participant_authority_fixture(root)
                valid = build_participant_derived_batch(root)
                if attack == "omitted":
                    forged = _forged_participant_batch(
                        valid, queries=valid.queries[:-1]
                    )
                elif attack == "extra":
                    source = valid.queries[-1]
                    pitcher_id = int(source.requested_range["pitcher_id"]) + 999_999
                    extra = dataclasses.replace(
                        source,
                        endpoint=(
                            "https://statsapi.mlb.com/api/v1/people/"
                            f"{pitcher_id}/stats"
                        ),
                        requested_range={
                            **source.requested_range,
                            "pitcher_id": pitcher_id,
                        },
                    )
                    forged = _forged_participant_batch(
                        valid, queries=(*valid.queries, extra)
                    )
                else:
                    forged = _forged_participant_batch(
                        valid, derived_from_sha256="0" * 64
                    )
                with self.assertRaises((ValueError, RuntimeError)):
                    publish_query_batch(root, forged)
                forged_id = _write_forged_canonical_batch(
                    root,
                    stage=forged.stage,
                    derived_from_sha256=forged.derived_from_sha256,
                    queries=forged.queries,
                )
                self.assertEqual(forged_id, forged.batch_id)
                with self.assertRaises((ValueError, RuntimeError)):
                    load_query_batch(root, forged_id)

    def test_participant_batch_final_authority_rebind_rejects_inventory_race(self):
        from mlb_backtest import v52_query_plan as query_plan

        for publication in ("new", "existing"):
            with self.subTest(publication=publication), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                inventory = _participant_authority_fixture(root)
                batch = query_plan.build_participant_derived_batch(root)
                final = (
                    root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{batch.batch_id}.json"
                )
                expected: bytes | None = None
                if publication == "existing":
                    query_plan.publish_query_batch(root, batch)
                    expected = final.read_bytes()

                def mutate_inventory() -> None:
                    payload = json.loads(inventory.path.read_bytes())
                    payload["counts"]["game_count"] += 1
                    inventory.path.write_bytes(_canonical_json_bytes(payload))

                with (
                    mock.patch.object(
                        query_plan,
                        "_before_query_batch_authority_rebind",
                        side_effect=mutate_inventory,
                    ) as hook,
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    query_plan.publish_query_batch(root, batch)
                hook.assert_called_once_with()
                if publication == "existing":
                    self.assertEqual(final.read_bytes(), expected)
                else:
                    self.assertFalse(final.exists())

    def test_participant_batch_publish_load_are_exactly_idempotent(self):
        from mlb_backtest.v52_query_plan import (
            build_participant_derived_batch,
            load_query_batch,
            publish_query_batch,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _participant_authority_fixture(root)
            batch = build_participant_derived_batch(root)
            path = publish_query_batch(root, batch)
            before = path.stat()
            self.assertEqual(publish_query_batch(root, batch), path)
            after = path.stat()
            self.assertEqual(
                (before.st_dev, before.st_ino),
                (after.st_dev, after.st_ino),
            )
            self.assertEqual(load_query_batch(root, batch.batch_id), batch)

    def test_bisect_and_legacy_share_strict_canonical_monthly_parent_validation(self):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
            bisect_statcast_query,
        )

        valid = _parent("2024-06-01", "2024-06-30")
        invalid = [
            dataclasses.replace(
                valid,
                parameters={k: v for k, v in valid.parameters.items() if k != "hfGT"},
            ),
            dataclasses.replace(
                valid, parameters={**valid.parameters, "extra": "filter"}
            ),
            dataclasses.replace(valid, parameters={**valid.parameters, "hfGT": "P|"}),
            _parent("2025-06-01", "2025-06-30"),
            _parent("2024-06-15", "2024-07-15"),
            dataclasses.replace(
                valid, requested_range={**valid.requested_range, "year": 2023}
            ),
        ]
        for parent in invalid:
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                bisect_statcast_query(parent)
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                _build_legacy_partition_batch_from_catalog(
                    [parent], [], protected_inventory_sha256="a" * 64
                )

        left, right = bisect_statcast_query(valid)
        for child in (left, right):
            self.assertEqual(
                set(child.parameters),
                {
                    "all",
                    "type",
                    "player_type",
                    "game_date_gt",
                    "game_date_lt",
                    "hfGT",
                    "group_by",
                    "sort_col",
                    "player_event_sort",
                    "sort_order",
                    "min_pitches",
                },
            )
            self.assertEqual(child.parameters["hfGT"], "R|")
            self.assertEqual(child.parent_query_id, logical_query_id(valid))

    def test_bisect_preserves_valid_promoted_lineage_and_rejects_malformed_metadata(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
            bisect_statcast_query,
        )

        parent = _parent("2024-06-01", "2024-06-30")
        promoted = _build_legacy_partition_batch_from_catalog(
            [parent],
            [_legacy("2024-06-01", "2024-06-30")],
            protected_inventory_sha256="a" * 64,
        ).queries[0]
        left, right = bisect_statcast_query(promoted)
        for child in (left, right):
            self.assertEqual(
                child.requested_range["protected_inventory_sha256"], "a" * 64
            )
            self.assertEqual(
                child.requested_range["acquisition"],
                "PROTECTED_LEGACY_REFERENCE",
            )
            self.assertEqual(child.parent_query_id, logical_query_id(promoted))

        for malformed in (None, 7, "a" * 63, "G" * 64):
            candidate = dataclasses.replace(
                promoted,
                requested_range={
                    **promoted.requested_range,
                    "protected_inventory_sha256": malformed,
                },
            )
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                bisect_statcast_query(candidate)

        for candidate in (
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "protected_inventory_sha256": "a" * 64,
                },
            ),
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "acquisition": "FETCH_REQUIRED",
                    "protected_inventory_sha256": "a" * 64,
                },
            ),
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                },
            ),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                bisect_statcast_query(candidate)

    def test_legacy_partition_maps_verified_children_and_fetches_only_uncovered_gaps(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
        )

        parent = _parent("2024-06-01", "2024-06-30")
        catalog = [
            _legacy("2024-06-01", "2024-06-10"),
            _legacy("2024-06-21", "2024-06-30"),
        ]
        batch = _build_legacy_partition_batch_from_catalog(
            [parent], catalog, protected_inventory_sha256="a" * 64
        )
        self.assertEqual(batch.derived_from_sha256, "a" * 64)
        self.assertEqual(
            [q.requested_range for q in batch.queries],
            [
                {
                    "year": 2024,
                    "start_date": "2024-06-01",
                    "end_date": "2024-06-10",
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                    "protected_inventory_sha256": "a" * 64,
                },
                {
                    "year": 2024,
                    "start_date": "2024-06-11",
                    "end_date": "2024-06-20",
                    "acquisition": "FETCH_REQUIRED",
                },
                {
                    "year": 2024,
                    "start_date": "2024-06-21",
                    "end_date": "2024-06-30",
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                    "protected_inventory_sha256": "a" * 64,
                },
            ],
        )
        self.assertTrue(
            all(q.parent_query_id == logical_query_id(parent) for q in batch.queries)
        )

    def test_legacy_partition_rejects_gap_overlap_and_mixed_parent_child_ambiguity(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
        )

        parent = _parent("2024-06-01", "2024-06-30")
        overlap = [
            _legacy("2024-06-01", "2024-06-20"),
            _legacy("2024-06-20", "2024-06-30"),
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            _build_legacy_partition_batch_from_catalog(
                [parent], overlap, protected_inventory_sha256="a" * 64
            )

        mixed = [
            _legacy("2024-06-01", "2024-06-30"),
            _legacy("2024-06-01", "2024-06-10"),
        ]
        with self.assertRaisesRegex(ValueError, "mixed parent/child"):
            _build_legacy_partition_batch_from_catalog(
                [parent], mixed, protected_inventory_sha256="a" * 64
            )

        bad_parent = dataclasses.replace(
            _parent("2024-07-01", "2024-07-31"),
            requested_range={
                "year": 2024,
                "start_date": "2024-07-02",
                "end_date": "2024-07-31",
            },
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
            _build_legacy_partition_batch_from_catalog(
                [bad_parent], [], protected_inventory_sha256="a" * 64
            )

    def test_legacy_partition_requires_exact_canonical_url_and_no_duplicate_mapping(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
        )

        parent = _parent("2024-06-01", "2024-06-30")
        malformed = _legacy("2024-06-01", "2024-06-10")
        malformed["canonical_query"] = {
            **malformed["canonical_query"],
            "url": "https://baseballsavant.mlb.com/statcast_search/csv?wrong=true",
        }
        with self.assertRaisesRegex(ValueError, "canonical URL"):
            _build_legacy_partition_batch_from_catalog(
                [parent], [malformed], protected_inventory_sha256="a" * 64
            )

        duplicate = [
            _legacy("2024-06-01", "2024-06-10", raw={"sha256": "1" * 64}),
            _legacy("2024-06-01", "2024-06-10", raw={"sha256": "2" * 64}),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _build_legacy_partition_batch_from_catalog(
                [parent], duplicate, protected_inventory_sha256="a" * 64
            )

    def test_legacy_partition_rejects_unsorted_unmapped_and_incompatible_children(self):
        from mlb_backtest.v52_query_plan import (
            _build_legacy_partition_batch_from_catalog,
        )

        june = _parent("2024-06-01", "2024-06-30")
        july = _parent("2024-07-01", "2024-07-31")
        unsorted = [
            _legacy("2024-06-21", "2024-06-30"),
            _legacy("2024-06-01", "2024-06-10"),
        ]
        with self.assertRaisesRegex(ValueError, "ordered"):
            _build_legacy_partition_batch_from_catalog(
                [june], unsorted, protected_inventory_sha256="a" * 64
            )

        for unmapped in (
            _legacy("2024-05-01", "2024-05-31"),
            _legacy("2024-06-25", "2024-07-05"),
            _legacy("2025-06-01", "2025-06-30"),
        ):
            with self.subTest(unmapped=unmapped), self.assertRaises(ValueError):
                _build_legacy_partition_batch_from_catalog(
                    [june, july], [unmapped], protected_inventory_sha256="a" * 64
                )

        incompatible = _legacy("2024-06-01", "2024-06-10")
        incompatible["canonical_query"] = {
            **incompatible["canonical_query"],
            "url": statcast_url("2024-06-01", "2024-06-10").replace(
                "hfGT=R%7C", "hfGT=P%7C"
            ),
        }
        with self.assertRaisesRegex(ValueError, "canonical URL"):
            _build_legacy_partition_batch_from_catalog(
                [june], [incompatible], protected_inventory_sha256="a" * 64
            )

    def test_legacy_partition_public_builder_replays_verified_decisions_exactly(self):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with self.subTest(public_load_contract="signature"):
            self.assertEqual(
                tuple(inspect.signature(query_plan.load_query_batch).parameters),
                ("repository_root", "batch_id", "read_only_progress"),
            )
        with self.subTest(public_load_contract="private_kwarg_rejected"):
            with self.assertRaises(TypeError):
                query_plan.load_query_batch(
                    Path("."), "a" * 64, _invoke_authority_hook=False
                )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, protected_sha256 = _legacy_decision_repository(
                Path(directory)
            )
            verified = publish_legacy_decision_inventory(root)
            root_batch = query_plan.build_statcast_root_batch(root)
            batch = query_plan.build_legacy_partition_batch(root)
            decisions = verified.payload["decisions"]

            self.assertEqual(batch.stage, "REMAINING")
            self.assertEqual(batch.derived_from_sha256, verified.sha256)
            self.assertEqual(len(batch.queries), len(decisions), 48)
            self.assertEqual(
                [logical_query_id(query) for query in batch.queries],
                [decision["candidate_query_id"] for decision in decisions],
            )
            self.assertEqual(
                [
                    (
                        decision["range"]["year"],
                        decision["range"]["start_date"],
                        decision["range"]["end_date"],
                        decision["candidate_query_id"],
                    )
                    for decision in decisions
                ],
                sorted(
                    (
                        decision["range"]["year"],
                        decision["range"]["start_date"],
                        decision["range"]["end_date"],
                        decision["candidate_query_id"],
                    )
                    for decision in decisions
                ),
            )

            disposition_counts = {"PROMOTED": 0, "FETCH_REQUIRED": 0}
            children_by_parent: dict[str, list[tuple[date, date]]] = {}
            parents_by_id = {
                logical_query_id(parent): parent for parent in root_batch.queries
            }
            for query, decision in zip(batch.queries, decisions):
                query_range = decision["range"]
                start = str(query_range["start_date"])
                end = str(query_range["end_date"])
                disposition = str(decision["disposition"])
                disposition_counts[disposition] += 1
                self.assertEqual(query.parent_query_id, decision["parent_query_id"])
                self.assertEqual(
                    (query.source, query.grain, query.endpoint),
                    (
                        "baseball_savant_statcast",
                        "pitch",
                        "https://baseballsavant.mlb.com/statcast_search/csv",
                    ),
                )
                self.assertEqual(
                    (
                        query.expected_media_type,
                        query.parser_version,
                        query.run_spec_sha256,
                        query.acquisition_contract_sha256,
                    ),
                    (
                        "text/csv",
                        "v5.2.1",
                        parents_by_id[query.parent_query_id or ""].run_spec_sha256,
                        parents_by_id[
                            query.parent_query_id or ""
                        ].acquisition_contract_sha256,
                    ),
                )
                self.assertEqual(query.parameters, _parent(start, end).parameters)
                expected_range: dict[str, object] = {
                    "year": query_range["year"],
                    "start_date": start,
                    "end_date": end,
                    "acquisition": (
                        "PROTECTED_LEGACY_REFERENCE"
                        if disposition == "PROMOTED"
                        else "FETCH_REQUIRED"
                    ),
                }
                if disposition == "PROMOTED":
                    expected_range["protected_inventory_sha256"] = protected_sha256
                self.assertEqual(query.requested_range, expected_range)
                children_by_parent.setdefault(query.parent_query_id or "", []).append(
                    (date.fromisoformat(start), date.fromisoformat(end))
                )

            self.assertEqual(disposition_counts, {"PROMOTED": 1, "FETCH_REQUIRED": 47})
            self.assertEqual(
                set(children_by_parent),
                {logical_query_id(parent) for parent in root_batch.queries},
            )
            for parent in root_batch.queries:
                ranges = children_by_parent[logical_query_id(parent)]
                parent_start = date.fromisoformat(
                    str(parent.requested_range["start_date"])
                )
                parent_end = date.fromisoformat(str(parent.requested_range["end_date"]))
                self.assertEqual(ranges[0][0], parent_start)
                self.assertEqual(ranges[-1][1], parent_end)
                self.assertTrue(
                    all(
                        current[0] == previous[1] + timedelta(days=1)
                        for previous, current in zip(ranges, ranges[1:])
                    )
                )

            with self.assertRaises(TypeError):
                query_plan.build_legacy_partition_batch(
                    root_batch.queries,
                    [],
                    protected_inventory_sha256=protected_sha256,
                )

    def test_legacy_partition_public_batch_is_idempotent_and_forgery_closed(self):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _protected_sha256 = _legacy_decision_repository(
                Path(directory)
            )
            verified = publish_legacy_decision_inventory(root)
            batch = query_plan.build_legacy_partition_batch(root)
            path = query_plan.publish_query_batch(root, batch)
            before = path.stat()
            self.assertEqual(query_plan.publish_query_batch(root, batch), path)
            after = path.stat()
            self.assertEqual(
                (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
            )
            self.assertEqual(query_plan.load_query_batch(root, batch.batch_id), batch)

            promoted_index = next(
                index
                for index, query in enumerate(batch.queries)
                if query.requested_range["acquisition"] == "PROTECTED_LEGACY_REFERENCE"
            )
            promoted = batch.queries[promoted_index]
            downgraded_range = dict(promoted.requested_range)
            downgraded_range["acquisition"] = "FETCH_REQUIRED"
            downgraded_range.pop("protected_inventory_sha256")
            downgraded = dataclasses.replace(promoted, requested_range=downgraded_range)
            candidate_index = next(
                index
                for index, query in enumerate(batch.queries)
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and date.fromisoformat(str(query.requested_range["end_date"]))
                > date.fromisoformat(str(query.requested_range["start_date"]))
            )
            changed_candidate = query_plan.bisect_statcast_query(
                batch.queries[candidate_index]
            )[0]
            subset_parent = batch.queries[0].parent_query_id
            single_parent_subset = tuple(
                query
                for query in batch.queries
                if query.parent_query_id == subset_parent
            )
            self.assertTrue(0 < len(single_parent_subset) < len(batch.queries))

            def replaced(index: int, query: LogicalQuery) -> tuple[LogicalQuery, ...]:
                values = list(batch.queries)
                values[index] = query
                return tuple(values)

            for label, derived, queries in (
                ("omit", verified.sha256, batch.queries[1:]),
                ("extra", verified.sha256, (*batch.queries, changed_candidate)),
                (
                    "reorder",
                    verified.sha256,
                    (batch.queries[1], batch.queries[0], *batch.queries[2:]),
                ),
                (
                    "disposition",
                    verified.sha256,
                    replaced(promoted_index, downgraded),
                ),
                (
                    "parent",
                    verified.sha256,
                    replaced(
                        0,
                        dataclasses.replace(batch.queries[0], parent_query_id="f" * 64),
                    ),
                ),
                (
                    "candidate",
                    verified.sha256,
                    replaced(candidate_index, changed_candidate),
                ),
                ("single_parent_subset", verified.sha256, single_parent_subset),
                ("derived", "f" * 64, batch.queries),
            ):
                with self.subTest(label=label):
                    forged = query_plan._make_batch("REMAINING", derived, queries)
                    with self.assertRaises((ValueError, RuntimeError)):
                        query_plan.publish_query_batch(root, forged)
                    forged_id = _write_forged_canonical_batch(
                        root,
                        stage="REMAINING",
                        derived_from_sha256=derived,
                        queries=queries,
                    )
                    with self.assertRaises((ValueError, RuntimeError)):
                        query_plan.load_query_batch(root, forged_id)

    def test_legacy_partition_authority_races_clean_up_and_recursive_children_pass(
        self,
    ):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from tests.test_v52_legacy_decision_inventory import (
            DECISION_PATH,
            _legacy_decision_repository,
            _statcast_root_batch_path,
        )

        for authority in ("decision", "root"):
            for publication in ("new", "existing"):
                with self.subTest(authority=authority, publication=publication):
                    with tempfile.TemporaryDirectory() as directory:
                        root, _inventory_path, _protected_sha256 = (
                            _legacy_decision_repository(Path(directory))
                        )
                        publish_legacy_decision_inventory(root)
                        batch = query_plan.build_legacy_partition_batch(root)
                        final = (
                            root
                            / "audit/v5_2_qd/historical/acquisition/query_batches"
                            / f"{batch.batch_id}.json"
                        )
                        expected: bytes | None = None
                        if publication == "existing":
                            query_plan.publish_query_batch(root, batch)
                            expected = final.read_bytes()

                        target = (
                            root / DECISION_PATH
                            if authority == "decision"
                            else _statcast_root_batch_path(root)
                        )

                        def mutate_authority() -> None:
                            payload = json.loads(target.read_bytes())
                            if authority == "decision":
                                payload["counts"]["fetch_required_count"] += 1
                            else:
                                payload["queries"] = payload["queries"][:-1]
                            target.write_bytes(_canonical_json_bytes(payload))

                        with (
                            mock.patch.object(
                                query_plan,
                                "_before_query_batch_authority_rebind",
                                side_effect=mutate_authority,
                            ) as hook,
                            self.assertRaises((ValueError, RuntimeError)),
                        ):
                            query_plan.publish_query_batch(root, batch)
                        hook.assert_called_once_with()
                        if publication == "existing":
                            self.assertEqual(final.read_bytes(), expected)
                        else:
                            self.assertFalse(final.exists())

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _protected_sha256 = _legacy_decision_repository(
                Path(directory)
            )
            publish_legacy_decision_inventory(root)
            batch = query_plan.build_legacy_partition_batch(root)
            query_plan.publish_query_batch(root, batch)
            parent = next(
                query
                for query in batch.queries
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and date.fromisoformat(str(query.requested_range["end_date"]))
                > date.fromisoformat(str(query.requested_range["start_date"]))
            )
            children = query_plan.bisect_statcast_query(parent)
            child_batch = query_plan._make_batch("REMAINING", batch.batch_id, children)
            query_plan.publish_query_batch(root, child_batch)
            self.assertEqual(
                query_plan.load_query_batch(root, child_batch.batch_id), child_batch
            )

    def test_batches_and_published_bytes_are_stable_closed_and_immutable(self):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        first = build_static_batch(RUN_SPEC, CONTRACT)
        second = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = publish_query_batch(root, first)
            original = path.read_bytes()
            self.assertTrue(original.endswith(b"\n"))
            self.assertFalse(original.endswith(b"\n\n"))
            record = json.loads(original)
            self.assertEqual(
                set(record),
                {
                    "schema_version",
                    "batch_id",
                    "stage",
                    "derived_from_sha256",
                    "queries",
                },
            )
            self.assertEqual(publish_query_batch(root, first), path)
            self.assertEqual(path.read_bytes(), original)
            with mock.patch.object(
                query_plan.os,
                "listdir",
                side_effect=AssertionError("batch load must not discover files"),
            ):
                self.assertEqual(load_query_batch(root, first.batch_id), first)
            conflicting = dataclasses.replace(first, stage="REMAINING")
            with self.assertRaisesRegex(ValueError, "REMAINING|Statcast"):
                publish_query_batch(root, conflicting)

    def test_load_query_batch_rejects_id_path_alias_and_canonical_binding_attacks(self):
        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        for bad_id in (batch.batch_id.upper(), "../" + batch.batch_id, "g" * 64):
            with self.subTest(bad_id=bad_id), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    load_query_batch(Path(td).resolve(), bad_id)

        for attack in ("embedded_id", "unknown", "query", "noncanonical"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                path = publish_query_batch(root, batch)
                record = json.loads(path.read_bytes())
                if attack == "embedded_id":
                    record["batch_id"] = "0" * 64
                    contents = _canonical_json_bytes(record)
                elif attack == "unknown":
                    record["unknown"] = True
                    contents = _canonical_json_bytes(record)
                elif attack == "query":
                    record["queries"][0]["parameters"]["sportId"] = 2.0
                    contents = _canonical_json_bytes(record)
                else:
                    contents = json.dumps(record, indent=2).encode() + b"\n"
                path.write_bytes(contents)
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    load_query_batch(root, batch.batch_id)

        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                path = publish_query_batch(root, batch)
                contents = path.read_bytes()
                path.unlink()
                target = root / "target.json"
                target.write_bytes(contents)
                if attack == "symlink":
                    path.symlink_to(target)
                else:
                    os.link(target, path)
                with self.assertRaises((ValueError, RuntimeError)):
                    load_query_batch(root, batch.batch_id)

    def test_load_query_batch_rejects_ancestor_replacement_during_read(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            query_plan.publish_query_batch(root, batch)
            original_read = query_plan._read_linked_regular
            replaced = False

            def replace_ancestor(*args: object, **kwargs: object):
                nonlocal replaced
                result = original_read(*args, **kwargs)  # type: ignore[arg-type]
                if not replaced:
                    replaced = True
                    (root / "audit").rename(root / "detached-audit")
                    (root / "audit").mkdir()
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_read_linked_regular",
                    side_effect=replace_ancestor,
                ),
                self.assertRaisesRegex(ValueError, "identity|changed|ancestor"),
            ):
                query_plan.load_query_batch(root, batch.batch_id)

    def test_load_query_batch_rejects_final_file_replacement_during_parse(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = query_plan.publish_query_batch(root, batch)
            original_parse = query_plan._parse_json_object
            replaced = False

            def replace_final(*args: object, **kwargs: object):
                nonlocal replaced
                result = original_parse(*args, **kwargs)  # type: ignore[arg-type]
                if not replaced:
                    replaced = True
                    contents = path.read_bytes()
                    path.unlink()
                    path.write_bytes(contents)
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_parse_json_object",
                    side_effect=replace_final,
                ),
                self.assertRaisesRegex(ValueError, "batch|identity|changed|replaced"),
            ):
                query_plan.load_query_batch(root, batch.batch_id)

    def test_publish_rejects_symlink_ancestors_and_hardlinked_existing_file(self):
        from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            actual_root = base / "actual"
            actual_root.mkdir()
            linked_root = base / "linked"
            linked_root.symlink_to(actual_root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root"):
                publish_query_batch(linked_root, batch)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            actual = base / "actual"
            nested = actual / "nested"
            nested.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root"):
                publish_query_batch(alias / "nested", batch)
            self.assertFalse((nested / "audit").exists())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            outside = root / "outside"
            outside.mkdir()
            (root / "audit").symlink_to(outside, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                publish_query_batch(root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = publish_query_batch(root, batch)
            alias = root / "alias"
            os.link(path, alias)
            with self.assertRaisesRegex(ValueError, "unalias"):
                publish_query_batch(root, batch)

    def test_publish_partial_temp_write_or_file_fsync_failure_leaves_no_final(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        for failure in ("write", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                destination = (
                    root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{batch.batch_id}.json"
                )
                original_write = os.write
                original_fsync = os.fsync
                file_calls = 0

                def failing_write(fd, data):
                    nonlocal file_calls
                    if os.path.isfile(f"/dev/fd/{fd}"):
                        file_calls += 1
                        if file_calls == 1:
                            return original_write(fd, bytes(data[:7]))
                        raise OSError("interrupted temp write")
                    return original_write(fd, data)

                def failing_fsync(fd):
                    if os.path.isfile(f"/dev/fd/{fd}"):
                        raise OSError("temp fsync failed")
                    return original_fsync(fd)

                patcher = mock.patch.object(
                    query_plan.os,
                    failure,
                    side_effect=failing_write if failure == "write" else failing_fsync,
                )
                with patcher, self.assertRaises(OSError):
                    query_plan.publish_query_batch(root, batch)
                self.assertFalse(destination.exists())
                batch_dir = destination.parent
                self.assertEqual(
                    list(batch_dir.glob("*.tmp")) if batch_dir.exists() else [], []
                )
                self.assertEqual(
                    query_plan.publish_query_batch(root, batch), destination
                )

    def test_publish_concurrent_identical_batches_are_idempotent(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            barrier = threading.Barrier(6)

            paths = []
            errors = []

            def worker():
                try:
                    barrier.wait(timeout=5)
                    paths.append(query_plan.publish_query_batch(root, batch))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(paths), 6)
            self.assertEqual(len(set(paths)), 1)

    def test_publish_final_file_exists_race_validates_identical_or_conflict(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as seed_td:
            expected = query_plan.publish_query_batch(
                Path(seed_td).resolve(), batch
            ).read_bytes()

        def race(root: Path, competing: bytes):
            def lose_link(_src, dst, *, src_dir_fd, dst_dir_fd, follow_symlinks):
                del src_dir_fd, follow_symlinks
                descriptor = os.open(
                    dst,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(descriptor, competing)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(dst)

            with mock.patch.object(query_plan.os, "link", side_effect=lose_link):
                return query_plan.publish_query_batch(root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = race(root, expected)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaisesRegex(RuntimeError, "differs"):
                race(root, b"{}\n")
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            self.assertEqual(list(batch_dir.glob("*.tmp")), [])

    def test_publish_serializes_the_post_link_window_before_reconciling(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            entered = threading.Event()
            release = threading.Event()
            second_done = threading.Event()
            original_unlink = query_plan._unlink_owned_temp
            pause_lock = threading.Lock()
            paused = False

            def pause_after_link(parent_fd, name, descriptor):
                nonlocal paused
                with pause_lock:
                    should_pause = not paused
                    if should_pause:
                        paused = True
                if should_pause:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release publisher")
                return original_unlink(parent_fd, name, descriptor)

            results = []
            errors = []

            def publish(*, mark_done=False):
                try:
                    results.append(query_plan.publish_query_batch(root, batch))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if mark_done:
                        second_done.set()

            with mock.patch.object(
                query_plan, "_unlink_owned_temp", side_effect=pause_after_link
            ):
                first = threading.Thread(target=publish)
                first.start()
                self.assertTrue(entered.wait(timeout=5))
                second = threading.Thread(target=publish, kwargs={"mark_done": True})
                second.start()
                second_was_blocked = not second_done.wait(timeout=0.1)
                release.set()
                first.join(timeout=10)
                second.join(timeout=10)

            self.assertTrue(second_was_blocked)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            final = results[0]
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertEqual(list(final.parent.glob("*.tmp")), [])

    def test_publish_retry_recovers_only_owned_stale_temp_hardlink(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with (
                mock.patch.object(
                    query_plan,
                    "_unlink_owned_temp",
                    side_effect=OSError("injected crash after link"),
                ),
                self.assertRaisesRegex(OSError, "injected crash"),
            ):
                query_plan.publish_query_batch(root, batch)

            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            final = batch_dir / f"{batch.batch_id}.json"
            stale = list(batch_dir.glob("*.tmp"))
            self.assertTrue(final.is_file())
            self.assertEqual(len(stale), 1)
            self.assertEqual(os.stat(final).st_nlink, 2)
            self.assertEqual(os.stat(stale[0]).st_ino, os.stat(final).st_ino)

            self.assertEqual(query_plan.publish_query_batch(root, batch), final)
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertEqual(list(batch_dir.glob("*.tmp")), [])

    def test_publish_recovery_rejects_conflicting_final_and_temp_without_deleting(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            final = batch_dir / f"{batch.batch_id}.json"
            final.write_bytes(b"{}\n")
            stale = batch_dir / f".{batch.batch_id}.{'b' * 32}.tmp"
            os.link(final, stale)

            with self.assertRaisesRegex(RuntimeError, "differs"):
                query_plan.publish_query_batch(root, batch)
            self.assertEqual(final.read_bytes(), b"{}\n")
            self.assertTrue(stale.exists())
            self.assertEqual(os.stat(final).st_nlink, 2)

    def test_publish_cleans_accumulated_prelink_private_temp_orphans(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as seed_td:
            expected = query_plan.publish_query_batch(
                Path(seed_td).resolve(), batch
            ).read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            stale = [
                batch_dir / f".{batch.batch_id}.{'1' * 32}.tmp",
                batch_dir / f".{batch.batch_id}.{'2' * 32}.tmp",
            ]
            stale[0].write_bytes(expected[:17])
            stale[1].write_bytes(expected)

            final = query_plan.publish_query_batch(root, batch)
            self.assertEqual(final.read_bytes(), expected)
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertFalse(any(path.exists() for path in stale))

    def test_publish_never_deletes_nonprivate_or_aliased_temp_entries(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        matching_name = f".{batch.batch_id}.{'c' * 32}.tmp"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            unrelated = batch_dir / ".unrelated.tmp"
            unrelated.write_bytes(b"keep")
            query_plan.publish_query_batch(root, batch)
            self.assertEqual(unrelated.read_bytes(), b"keep")

        for attack in ("symlink", "hardlink", "fifo"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
                batch_dir.mkdir(parents=True)
                candidate = batch_dir / matching_name
                target = root / "target"
                if attack == "symlink":
                    target.write_bytes(b"keep")
                    candidate.symlink_to(target)
                elif attack == "hardlink":
                    target.write_bytes(b"keep")
                    os.link(target, candidate)
                else:
                    os.mkfifo(candidate)

                with self.assertRaises(ValueError):
                    query_plan.publish_query_batch(root, batch)
                self.assertTrue(os.path.lexists(candidate))
                final = batch_dir / f"{batch.batch_id}.json"
                self.assertFalse(final.exists())

    def test_split_receipt_publishes_closed_canonical_parent_bound_record(self):
        import jsonschema

        from mlb_backtest.v52_query_plan import (
            SplitReceipt,
            load_split_receipt,
            publish_split_receipt,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            receipt = publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )

            self.assertIsInstance(receipt, SplitReceipt)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                receipt.depth = 99  # type: ignore[misc]
            expected_path = (
                root
                / "audit/v5_2_qd/historical/acquisition/split_receipts"
                / f"{logical_query_id(parent)}.json"
            )
            contents = expected_path.read_bytes()
            record = json.loads(contents)
            schema_path = ROOT / "schemas/v5_2_split_receipt.schema.json"
            schema_contents = schema_path.read_bytes()
            schema = json.loads(schema_contents)
            jsonschema.Draft202012Validator(schema).validate(record)
            self.assertEqual(
                schema_contents,
                _canonical_json_bytes(schema),
                "the closed schema itself must be canonical",
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(receipt.receipt_sha256, hashlib.sha256(contents).hexdigest())
            self.assertEqual(receipt.stage, "REMAINING")
            self.assertEqual(receipt.parent_batch_id, parent_batch.batch_id)
            self.assertEqual(receipt.parent_query_id, logical_query_id(parent))
            self.assertEqual(receipt.reason, "TIMEOUT")
            self.assertEqual(receipt.exhausted_attempt_number, attempt_number)
            self.assertEqual(receipt.depth, 1)
            self.assertEqual(receipt.child_batch_id, child_batch.batch_id)
            self.assertEqual(
                receipt.recorded_at_utc,
                "2026-07-14T12:01:00.000000Z",
                "recorded_at_utc is the deterministic split decision timestamp",
            )
            self.assertEqual(
                receipt.child_query_ids,
                tuple(logical_query_id(query) for query in child_batch.queries),
            )
            self.assertEqual(
                load_split_receipt(
                    root,
                    logical_query_id(parent),
                    evidence_root=_split_evidence_root(root),
                ),
                receipt,
            )
            self.assertIsNone(
                load_split_receipt(
                    root,
                    "f" * 64,
                    evidence_root=_split_evidence_root(root),
                )
            )

    def test_split_receipt_rejects_generic_bodyless_split_attempts(self):
        import mlb_backtest.v52_query_plan as query_plan

        for reason in ("TIMEOUT", "SIZE_LIMIT", "ROW_CAP"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                _statcast_authority_fixture(root, in_place=True)
                parent_batch = query_plan.build_statcast_root_batch(root)
                parent = next(
                    query
                    for query in parent_batch.queries
                    if query.requested_range["start_date"]
                    != query.requested_range["end_date"]
                )
                child_batch = query_plan.build_statcast_split_batch(
                    root, parent_batch, parent
                )
                for batch in (parent_batch, child_batch):
                    query_plan.publish_query_batch(root, batch)
                attempt_number = _forge_generic_split_attempt(
                    root, parent, reason
                )

                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "split evidence|failure evidence|quarantine|body|dedicated",
                ):
                    query_plan.publish_split_receipt(
                        root,
                        evidence_root=_split_evidence_root(root),
                        parent_batch_id=parent_batch.batch_id,
                        parent_query_id=logical_query_id(parent),
                        reason=reason,
                        exhausted_attempt_number=attempt_number,
                        child_batch_id=child_batch.batch_id,
                    )

    def test_split_receipt_rejects_missing_or_tampered_partial_evidence(self):
        import mlb_backtest.v52_query_plan as query_plan

        for attack in (
            "missing_receipt",
            "tampered_receipt",
            "missing_body",
            "tampered_body",
        ):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                parent_batch, parent, child_batch, _ = _split_batches(root)
                store = V52EvidenceStore(_split_evidence_root(root))
                with store.acquire_query(parent) as lease:
                    attempt_number = store.next_attempt_number(lease)
                    evidence = store.persist_split_attempt(
                        parent,
                        attempt_number,
                        _split_capture(parent, "SIZE_LIMIT"),
                        reason="SIZE_LIMIT",
                    )
                assert evidence.body_path is not None
                if attack == "missing_receipt":
                    evidence.evidence_receipt_path.unlink()
                elif attack == "tampered_receipt":
                    receipt = json.loads(evidence.evidence_receipt_path.read_bytes())
                    receipt["raw_sha256"] = "f" * 64
                    evidence.evidence_receipt_path.write_bytes(
                        _canonical_json_bytes(receipt)
                    )
                elif attack == "missing_body":
                    evidence.body_path.unlink()
                else:
                    evidence.body_path.write_bytes(b"tampered partial response")

                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "split evidence|failure evidence|quarantine|body|hash|receipt",
                ):
                    query_plan.publish_split_receipt(
                        root,
                        evidence_root=_split_evidence_root(root),
                        parent_batch_id=parent_batch.batch_id,
                        parent_query_id=logical_query_id(parent),
                        reason="SIZE_LIMIT",
                        exhausted_attempt_number=attempt_number,
                        child_batch_id=child_batch.batch_id,
                    )

    def test_split_receipt_accepts_dedicated_bodyless_timeout_evidence(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            self.assertEqual(receipt.reason, "TIMEOUT")
            self.assertEqual(receipt.exhausted_attempt_number, attempt_number)

    def test_split_receipt_requires_authenticated_run_spec_raw_evidence_root(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            repository_root = Path(td).resolve()
            evidence_root = _split_evidence_root(repository_root)
            parent_batch, parent, child_batch, attempt_number = _split_batches(
                repository_root
            )
            kwargs = {
                "parent_batch_id": parent_batch.batch_id,
                "parent_query_id": logical_query_id(parent),
                "reason": "TIMEOUT",
                "exhausted_attempt_number": attempt_number,
                "child_batch_id": child_batch.batch_id,
            }
            wrong_evidence_root = repository_root / "wrong-raw"
            wrong_evidence_root.mkdir()

            with self.assertRaisesRegex(ValueError, "evidence root|RunSpec|raw"):
                query_plan.publish_split_receipt(
                    repository_root,
                    evidence_root=wrong_evidence_root,
                    **kwargs,
                )
            run_spec_path = repository_root / query_plan._RUN_SPEC_RELATIVE
            authenticated_run_spec = run_spec_path.read_bytes()
            forged_run_spec = json.loads(authenticated_run_spec)
            forged_run_spec["label"] = "forged split RunSpec"
            run_spec_path.write_bytes(_canonical_json_bytes(forged_run_spec))
            with self.assertRaisesRegex(
                ValueError, "RunSpec hash binding|historical run|frozen values"
            ):
                query_plan.publish_split_receipt(
                    repository_root,
                    evidence_root=evidence_root,
                    **kwargs,
                )
            run_spec_path.write_bytes(authenticated_run_spec)
            receipt = query_plan.publish_split_receipt(
                repository_root,
                evidence_root=evidence_root,
                **kwargs,
            )
            self.assertEqual(
                query_plan.load_split_receipt(
                    repository_root,
                    receipt.parent_query_id,
                    evidence_root=evidence_root,
                ),
                receipt,
            )
            with self.assertRaisesRegex(ValueError, "evidence root|RunSpec|raw"):
                query_plan.load_split_receipt(
                    repository_root,
                    receipt.parent_query_id,
                    evidence_root=wrong_evidence_root,
                )

    def test_split_receipt_rejects_consistent_forged_contract_binding(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _statcast_authority_fixture(root, in_place=True)
            evidence_root = _split_evidence_root(root)
            legitimate = query_plan._build_statcast_root_batch(RUN_SPEC, CONTRACT)
            forged_queries = tuple(
                dataclasses.replace(
                    query,
                    acquisition_contract_sha256="f" * 64,
                )
                for query in legitimate.queries
            )
            parent_batch = query_plan._make_batch(
                "REMAINING", None, forged_queries
            )
            parent = next(
                query
                for query in parent_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            child_batch = query_plan._make_batch(
                "REMAINING",
                parent_batch.batch_id,
                query_plan.bisect_statcast_query(parent),
            )
            forged_id = _write_forged_canonical_batch(
                root,
                stage="REMAINING",
                derived_from_sha256=None,
                queries=parent_batch.queries,
            )
            self.assertEqual(forged_id, parent_batch.batch_id)
            query_plan.publish_query_batch(root, child_batch)
            attempt_number = _record_split_attempt(root, parent, "TIMEOUT")

            with self.assertRaisesRegex(
                ValueError,
                "acquisition contract|fixed contract replay|"
                "fixed Statcast authority replay",
            ):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=evidence_root,
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=logical_query_id(parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                )

    def test_split_receipt_reauthenticates_repository_contract_on_publish_and_load(
        self,
    ):
        import mlb_backtest.v52_query_plan as query_plan

        mutations = (
            "missing",
            "symlink",
            "noncanonical",
            "run_path_traversal",
            "run_path_absolute",
            "bytes_hash_mismatch",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                evidence_root = _split_evidence_root(root)
                parent_batch, parent, child_batch, attempt_number = _split_batches(
                    root
                )
                kwargs = {
                    "evidence_root": evidence_root,
                    "parent_batch_id": parent_batch.batch_id,
                    "parent_query_id": logical_query_id(parent),
                    "reason": "TIMEOUT",
                    "exhausted_attempt_number": attempt_number,
                    "child_batch_id": child_batch.batch_id,
                }
                receipt = query_plan.publish_split_receipt(root, **kwargs)
                contract_path = (
                    root
                    / "protocols/v5_2_qd/historical_source_acquisition.json"
                )
                if mutation == "missing":
                    contract_path.unlink()
                elif mutation == "symlink":
                    target = root / "contract-target.json"
                    target.write_bytes(contract_path.read_bytes())
                    contract_path.unlink()
                    contract_path.symlink_to(target)
                elif mutation == "noncanonical":
                    contract_path.write_text(
                        json.dumps(json.loads(contract_path.read_bytes()), indent=2)
                    )
                else:
                    contract = json.loads(contract_path.read_bytes())
                    if mutation == "run_path_traversal":
                        contract["bindings"]["run_spec"]["path"] = (
                            "../historical_walkforward_500.json"
                        )
                    elif mutation == "run_path_absolute":
                        contract["bindings"]["run_spec"]["path"] = str(
                            root / query_plan._RUN_SPEC_RELATIVE
                        )
                    else:
                        contract["minimum_free_space_reserve_bytes"] += 1
                    contract_path.write_bytes(_canonical_json_bytes(contract))

                expected_error = (
                    (FileNotFoundError, ValueError)
                    if mutation == "missing"
                    else ValueError
                )
                with self.assertRaisesRegex(
                    expected_error,
                    "acquisition contract|historical_source_acquisition|RunSpec",
                ):
                    query_plan.publish_split_receipt(root, **kwargs)
                with self.assertRaisesRegex(
                    expected_error,
                    "acquisition contract|historical_source_acquisition|RunSpec",
                ):
                    query_plan.load_split_receipt(
                        root,
                        receipt.parent_query_id,
                        evidence_root=evidence_root,
                    )

    def test_split_receipt_attempt_append_race_cleans_owned_final(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            evidence_root = _split_evidence_root(root)
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            parent_query_id = logical_query_id(parent)
            original_publish = query_plan._publish_split_bytes_locked
            appended = False

            def append_attempt_after_final(*args, **kwargs):
                nonlocal appended
                result = original_publish(*args, **kwargs)
                if not appended:
                    appended = True
                    _record_split_attempt(root, parent, "TIMEOUT")
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_publish_split_bytes_locked",
                    side_effect=append_attempt_after_final,
                ),
                self.assertRaisesRegex(ValueError, "latest attempt|membership"),
            ):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=evidence_root,
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=parent_query_id,
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                )
            final = (
                root
                / "audit/v5_2_qd/historical/acquisition/split_receipts"
                / f"{parent_query_id}.json"
            )
            self.assertFalse(final.exists(), "a stale immutable final poisons retries")

    def test_split_receipt_rejects_child_acquisition_metadata_injection(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            forged_children = list(child_batch.queries)
            requested = dict(forged_children[0].requested_range)
            requested.update(
                {
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                    "protected_inventory_sha256": "a" * 64,
                }
            )
            forged_children[0] = dataclasses.replace(
                forged_children[0], requested_range=requested
            )
            forged = query_plan._make_batch(
                "REMAINING", parent_batch.batch_id, forged_children
            )
            query_plan.publish_query_batch(root, forged)

            with self.assertRaisesRegex(ValueError, "canonical|child query"):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=logical_query_id(parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=forged.batch_id,
                )

    def test_split_receipt_schema_rejects_duplicate_children_and_cycle_guard(self):
        import mlb_backtest.v52_query_plan as query_plan

        schema = json.loads(
            (ROOT / "schemas/v5_2_split_receipt.schema.json").read_bytes()
        )
        self.assertTrue(
            schema["properties"]["children"].get("uniqueItems"),
            "duplicate child entries must fail the schema",
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            descriptors = query_plan._open_split_namespace(root, create=False)
            self.assertIsNotNone(descriptors)
            assert descriptors is not None
            try:
                with self.assertRaisesRegex(ValueError, "cycle"):
                    query_plan._load_split_receipt_locked(
                        root,
                        _split_evidence_root(root),
                        descriptors[-1],
                        logical_query_id(parent),
                        active_parent_ids={logical_query_id(parent)},
                    )
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)

    def test_split_receipt_requires_child_first_latest_attempt_and_exact_lineage(self):
        import mlb_backtest.v52_query_plan as query_plan

        mutations = (
            "missing_child",
            "reason",
            "attempt",
            "derived_from",
            "coverage",
            "run_binding",
            "parent_binding",
            "stage",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                parent_batch, parent, child_batch, attempt_number = _split_batches(root)
                child_batch_id = child_batch.batch_id
                reason = "TIMEOUT"
                exhausted_attempt_number = attempt_number
                if mutation == "missing_child":
                    child_batch_id = "f" * 64
                elif mutation == "reason":
                    reason = "SIZE_LIMIT"
                elif mutation == "attempt":
                    exhausted_attempt_number += 1
                else:
                    children = child_batch.queries
                    stage = "REMAINING"
                    derived_from = parent_batch.batch_id
                    if mutation == "derived_from":
                        derived_from = "e" * 64
                    elif mutation == "coverage":
                        children = children[:1]
                    elif mutation == "run_binding":
                        children = tuple(
                            dataclasses.replace(query, run_spec_sha256="d" * 64)
                            for query in children
                        )
                    elif mutation == "parent_binding":
                        children = tuple(
                            dataclasses.replace(query, parent_query_id="c" * 64)
                            for query in children
                        )
                    elif mutation == "stage":
                        stage = "REFERENCE_INPUTS"
                    if mutation == "stage":
                        child_batch_id = _write_forged_canonical_batch(
                            root,
                            stage=stage,
                            derived_from_sha256=derived_from,
                            queries=tuple(children),
                        )
                    else:
                        forged = query_plan._make_batch(
                            stage, derived_from, children
                        )
                        query_plan.publish_query_batch(root, forged)
                        child_batch_id = forged.batch_id

                with self.assertRaises((ValueError, RuntimeError)):
                    query_plan.publish_split_receipt(
                        root,
                        evidence_root=_split_evidence_root(root),
                        parent_batch_id=parent_batch.batch_id,
                        parent_query_id=logical_query_id(parent),
                        reason=reason,
                        exhausted_attempt_number=exhausted_attempt_number,
                        child_batch_id=child_batch_id,
                    )
                receipt_path = (
                    root
                    / "audit/v5_2_qd/historical/acquisition/split_receipts"
                    / f"{logical_query_id(parent)}.json"
                )
                self.assertFalse(receipt_path.exists())

    def test_split_receipt_rejects_noncanonical_three_way_children(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            left, right = child_batch.queries
            left_left, left_right = query_plan.bisect_statcast_query(left)
            parent_query_id = logical_query_id(parent)
            noncanonical = query_plan._make_batch(
                "REMAINING",
                parent_batch.batch_id,
                (
                    dataclasses.replace(
                        left_left, parent_query_id=parent_query_id
                    ),
                    dataclasses.replace(
                        left_right, parent_query_id=parent_query_id
                    ),
                    right,
                ),
            )
            query_plan.publish_query_batch(root, noncanonical)

            with self.assertRaisesRegex(ValueError, "canonical.*bisect|child batch"):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=logical_query_id(parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=noncanonical.batch_id,
                )

    def test_split_receipt_proves_recursive_depth_from_root_and_legacy_anchors(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            root_receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            recursive_parent = next(
                query
                for query in child_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 1
            )
            recursive_children = query_plan.bisect_statcast_query(recursive_parent)
            recursive_batch = query_plan._make_batch(
                "REMAINING", child_batch.batch_id, recursive_children
            )
            query_plan.publish_query_batch(root, recursive_batch)
            recursive_attempt = _record_split_attempt(root, recursive_parent, "ROW_CAP")
            recursive_receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=child_batch.batch_id,
                parent_query_id=logical_query_id(recursive_parent),
                reason="ROW_CAP",
                exhausted_attempt_number=recursive_attempt,
                child_batch_id=recursive_batch.batch_id,
            )
            self.assertEqual(root_receipt.depth, 1)
            self.assertEqual(recursive_receipt.depth, 2)
            self.assertEqual(
                query_plan.load_split_receipt(
                    root,
                    logical_query_id(recursive_parent),
                    evidence_root=_split_evidence_root(root),
                ),
                recursive_receipt,
            )

        with tempfile.TemporaryDirectory() as td:
            from mlb_backtest.v52_legacy_decision_inventory import (
                publish_legacy_decision_inventory,
            )
            from tests.test_v52_legacy_decision_inventory import (
                _legacy_decision_repository,
            )

            root, _inventory_path, _protected_sha256 = (
                _legacy_decision_repository(Path(td))
            )
            publish_legacy_decision_inventory(root)
            legacy_batch = query_plan.build_legacy_partition_batch(root)
            legacy_parent = next(
                query
                for query in legacy_batch.queries
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            children = query_plan.bisect_statcast_query(legacy_parent)
            child_batch = query_plan._make_batch(
                "REMAINING", legacy_batch.batch_id, children
            )
            for batch in (legacy_batch, child_batch):
                query_plan.publish_query_batch(root, batch)
            attempt_number = _record_split_attempt(root, legacy_parent, "SIZE_LIMIT")
            legacy_receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=legacy_batch.batch_id,
                parent_query_id=logical_query_id(legacy_parent),
                reason="SIZE_LIMIT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            recursive_parent = next(
                query
                for query in child_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 1
            )
            recursive_batch = query_plan._make_batch(
                "REMAINING",
                child_batch.batch_id,
                query_plan.bisect_statcast_query(recursive_parent),
            )
            query_plan.publish_query_batch(root, recursive_batch)
            recursive_attempt = _record_split_attempt(
                root, recursive_parent, "ROW_CAP"
            )
            recursive_receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=child_batch.batch_id,
                parent_query_id=logical_query_id(recursive_parent),
                reason="ROW_CAP",
                exhausted_attempt_number=recursive_attempt,
                child_batch_id=recursive_batch.batch_id,
            )
            self.assertEqual(legacy_receipt.depth, 1)
            self.assertEqual(recursive_receipt.depth, 2)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _statcast_authority_fixture(root, in_place=True)
            root_batch = query_plan.build_statcast_root_batch(root)
            root_parent = next(
                query
                for query in root_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 6
            )
            query_plan.publish_query_batch(root, root_batch)
            arbitrary_batch = query_plan.build_statcast_split_batch(
                root, root_batch, root_parent
            )
            query_plan.publish_query_batch(root, arbitrary_batch)
            arbitrary_parent = next(
                query
                for query in arbitrary_batch.queries
                if query.requested_range["start_date"]
                != query.requested_range["end_date"]
            )
            arbitrary_children = query_plan.build_statcast_split_batch(
                root, arbitrary_batch, arbitrary_parent
            )
            query_plan.publish_query_batch(root, arbitrary_children)
            arbitrary_attempt = _record_split_attempt(
                root, arbitrary_parent, "TIMEOUT"
            )
            with self.assertRaisesRegex(ValueError, "ancestor receipt"):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=arbitrary_batch.batch_id,
                    parent_query_id=logical_query_id(arbitrary_parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=arbitrary_attempt,
                    child_batch_id=arbitrary_children.batch_id,
                )

    def test_split_receipt_publication_is_idempotent_and_race_safe(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            kwargs = {
                "evidence_root": _split_evidence_root(root),
                "parent_batch_id": parent_batch.batch_id,
                "parent_query_id": logical_query_id(parent),
                "reason": "TIMEOUT",
                "exhausted_attempt_number": attempt_number,
                "child_batch_id": child_batch.batch_id,
            }
            first = query_plan.publish_split_receipt(root, **kwargs)
            self.assertEqual(query_plan.publish_split_receipt(root, **kwargs), first)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            barrier = threading.Barrier(3)
            results = []
            errors = []

            def publish() -> None:
                try:
                    barrier.wait(timeout=10)
                    results.append(
                        query_plan.publish_split_receipt(
                            root,
                            evidence_root=_split_evidence_root(root),
                            parent_batch_id=parent_batch.batch_id,
                            parent_query_id=logical_query_id(parent),
                            reason="TIMEOUT",
                            exhausted_attempt_number=attempt_number,
                            child_batch_id=child_batch.batch_id,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])

    def test_split_receipt_rejects_noncanonical_daily_partition(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            first, last = query_plan.validate_statcast_query(parent)
            daily_children = []
            cursor = first
            while cursor <= last:
                daily_children.append(query_plan._statcast_child(parent, cursor, cursor))
                cursor += timedelta(days=1)
            competing = query_plan._make_batch(
                "REMAINING", parent_batch.batch_id, daily_children
            )
            query_plan.publish_query_batch(root, competing)
            query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            with self.assertRaisesRegex(ValueError, "canonical bisection"):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=logical_query_id(parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=competing.batch_id,
                )

    def test_split_receipt_fsync_failure_leaves_no_final_or_private_temp(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            receipt_dir = (
                root / "audit/v5_2_qd/historical/acquisition/split_receipts"
            )
            parent_query_id = logical_query_id(parent)
            original_fsync = query_plan.os.fsync

            def fail_receipt_file_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                if stat.S_ISREG(metadata.st_mode):
                    raise OSError("injected split receipt fsync failure")
                original_fsync(descriptor)

            with (
                mock.patch.object(
                    query_plan.os,
                    "fsync",
                    side_effect=fail_receipt_file_fsync,
                ),
                self.assertRaisesRegex(OSError, "injected split receipt"),
            ):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=parent_query_id,
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                )
            self.assertFalse((receipt_dir / f"{parent_query_id}.json").exists())
            self.assertEqual(list(receipt_dir.glob("*.tmp")), [])
            self.assertEqual(
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=parent_query_id,
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                ).parent_query_id,
                parent_query_id,
            )

    def test_split_receipt_retry_recovers_lost_ack_after_final_link(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            parent_query_id = logical_query_id(parent)
            receipt_dir = (
                root / "audit/v5_2_qd/historical/acquisition/split_receipts"
            )
            with (
                mock.patch.object(
                    query_plan,
                    "_unlink_owned_temp",
                    side_effect=OSError("injected lost acknowledgement"),
                ),
                self.assertRaisesRegex(OSError, "lost acknowledgement"),
            ):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=parent_query_id,
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                )
            final = receipt_dir / f"{parent_query_id}.json"
            stale = list(receipt_dir.glob("*.tmp"))
            self.assertTrue(final.is_file())
            self.assertEqual(len(stale), 1)
            self.assertEqual(os.stat(final).st_ino, os.stat(stale[0]).st_ino)

            recovered = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=parent_query_id,
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            self.assertEqual(recovered.parent_query_id, parent_query_id)
            self.assertEqual(recovered.recorded_at_utc, "2026-07-14T12:01:00.000000Z")
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertEqual(list(receipt_dir.glob("*.tmp")), [])

    def test_recursive_split_load_rejects_corrupted_ancestor_receipt(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            root_receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            recursive_parent = next(
                query
                for query in child_batch.queries
                if (
                    date.fromisoformat(str(query.requested_range["end_date"]))
                    - date.fromisoformat(str(query.requested_range["start_date"]))
                ).days
                >= 1
            )
            recursive_batch = query_plan._make_batch(
                "REMAINING",
                child_batch.batch_id,
                query_plan.bisect_statcast_query(recursive_parent),
            )
            query_plan.publish_query_batch(root, recursive_batch)
            recursive_attempt = _record_split_attempt(root, recursive_parent, "ROW_CAP")
            query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=child_batch.batch_id,
                parent_query_id=logical_query_id(recursive_parent),
                reason="ROW_CAP",
                exhausted_attempt_number=recursive_attempt,
                child_batch_id=recursive_batch.batch_id,
            )
            ancestor_path = (
                root
                / "audit/v5_2_qd/historical/acquisition/split_receipts"
                / f"{root_receipt.parent_query_id}.json"
            )
            forged = json.loads(ancestor_path.read_bytes())
            forged["depth"] = 99
            identity = dict(forged)
            identity.pop("split_receipt_id")
            forged["split_receipt_id"] = hashlib.sha256(
                _canonical_json_bytes(identity).removesuffix(b"\n")
            ).hexdigest()
            ancestor_path.write_bytes(_canonical_json_bytes(forged))
            with self.assertRaisesRegex(ValueError, "reconstruction|ancestor|depth"):
                query_plan.load_split_receipt(
                    root,
                    logical_query_id(recursive_parent),
                    evidence_root=_split_evidence_root(root),
                )

    def test_split_receipt_rejects_unsafe_namespace_and_rebind(self):
        import mlb_backtest.v52_query_plan as query_plan

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            external = root / "external"
            external.mkdir()
            (acquisition / "split_receipts").symlink_to(
                external, target_is_directory=True
            )
            with self.assertRaisesRegex(ValueError, "no-follow|ancestor|directory"):
                query_plan.publish_split_receipt(
                    root,
                    evidence_root=_split_evidence_root(root),
                    parent_batch_id=parent_batch.batch_id,
                    parent_query_id=logical_query_id(parent),
                    reason="TIMEOUT",
                    exhausted_attempt_number=attempt_number,
                    child_batch_id=child_batch.batch_id,
                )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            parent_batch, parent, child_batch, attempt_number = _split_batches(root)
            receipt = query_plan.publish_split_receipt(
                root,
                evidence_root=_split_evidence_root(root),
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=logical_query_id(parent),
                reason="TIMEOUT",
                exhausted_attempt_number=attempt_number,
                child_batch_id=child_batch.batch_id,
            )
            receipt_path = (
                root
                / "audit/v5_2_qd/historical/acquisition/split_receipts"
                / f"{receipt.parent_query_id}.json"
            )
            replacement = receipt_path.with_name("replacement.json")
            replacement.write_bytes(receipt_path.read_bytes())
            original = query_plan._read_linked_regular
            replaced = False

            def replace_after_read(parent_fd: int, name: str, label: str):
                nonlocal replaced
                result = original(parent_fd, name, label)
                if label == "split receipt" and result is not None and not replaced:
                    replaced = True
                    os.replace(replacement, receipt_path)
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_read_linked_regular",
                    side_effect=replace_after_read,
                ),
                self.assertRaisesRegex(ValueError, "identity|changed"),
            ):
                query_plan.load_split_receipt(
                    root,
                    receipt.parent_query_id,
                    evidence_root=_split_evidence_root(root),
                )


if __name__ == "__main__":
    unittest.main()
