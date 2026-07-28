from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from mlb_backtest.protocols import load_run_spec, load_source_run_spec
from mlb_backtest.v52_evidence import AttemptCapture, V52EvidenceStore, logical_query_id
from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch
from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC_PATH = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT_PATH = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
SCHEMA_PATH = ROOT / "schemas/v5_2_schedule_anchor_inventory.schema.json"
NEUTRALIZATION_FIXTURE = (
    ROOT / "tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json"
)
INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
SOURCE_RUN_SPEC_PATH = (
    ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
SOURCE_CONTRACT_PATH = (
    ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
SOURCE_ADDENDUM_PATH = Path(
    "docs/superpowers/specs/"
    "2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md"
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
    SOURCE_ADDENDUM_PATH,
)
_ACTIVE_LOCAL_DISPOSITION_BOUNDARY = None


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


@dataclasses.dataclass(frozen=True)
class _SourceFixture:
    root: Path
    evidence_root: Path
    run_spec: object
    contract_path: Path
    batch_id: str


@contextmanager
def _local_disposition_fixture_boundary():
    """Scope a synthetic disposition to temporary fixture repositories only.

    Production's fixed-ID, 21MB forensic verifier remains covered by
    ``test_v52_source_schedule``.  This boundary supplies only the small,
    raw-less disposition needed by downstream lifecycle fixtures.
    """
    from mlb_backtest import v52_duplicate_gamepk_disposition as disposition
    from mlb_backtest import v52_query_plan
    from mlb_backtest.v52_evidence import LogicalQuery

    global _ACTIVE_LOCAL_DISPOSITION_BOUNDARY
    if _ACTIVE_LOCAL_DISPOSITION_BOUNDARY is not None:
        yield _ACTIVE_LOCAL_DISPOSITION_BOUNDARY
        return

    authorities: dict[Path, object] = {}
    projection = disposition.ScheduleOccurrenceProjection(
        occurrence_counts=disposition.EXPECTED_OCCURRENCE_COUNTS,
        suppressed_position=disposition.SUPPRESSED_POSITION,
        retained_position=disposition.RETAINED_POSITION,
        normalized_games=(
            {
                "away_is_winner": True,
                "away_score": 7,
                "away_team_id": 133,
                "completed_innings": 9,
                "doubleheader_marker": None,
                "game_pk": 565534,
                "home_is_winner": False,
                "home_score": 3,
                "home_team_id": 116,
                "official_date": "2019-05-19",
                "scheduled_innings": 9,
                "scheduled_start_utc": "2019-09-07T00:15:00Z",
                "status": "Final",
                "venue_id": 10,
            },
        ),
        selected_raw_game_object_sha256s=(
            {
                "game_pk": 565534,
                "raw_game_object_sha256": disposition.RETAINED_OBJECT_SHA256,
            },
        ),
        site_flags=(
            {"game_pk": 565534, "neutral_site": False, "alternate_site": False},
        ),
    )
    real_build = disposition.build_duplicate_disposition_authority
    real_recompute = disposition.DuplicateGamePkDispositionAuthority.recompute

    def build_fixture_authority(
        root: Path,
        *,
        source_run_spec_path: Path,
        acquisition_contract_path: Path,
    ) -> object:
        fixture = authorities.get(Path(root).resolve())
        if fixture is None:
            return real_build(
                root,
                source_run_spec_path=source_run_spec_path,
                acquisition_contract_path=acquisition_contract_path,
            )
        if fixture.source_run_spec_path != Path(
            source_run_spec_path
        ) or fixture.acquisition_contract_path != Path(acquisition_contract_path):
            raise ValueError("fixture disposition authority path differs")
        return fixture

    def recompute_fixture_authority(authority: object) -> object:
        if authorities.get(authority.repository_root) == authority:
            return projection
        return real_recompute(authority)

    class Boundary:
        def install(
            self, root: Path, source_run_path: Path, contract_path: Path
        ) -> object:
            root = root.resolve()
            context = v52_query_plan._load_static_contracts(
                source_run_path, contract_path
            )
            query = LogicalQuery(
                source="mlb_schedule_game_feed",
                grain="season_schedule_and_game",
                endpoint=disposition.DISPOSITION_ENDPOINT,
                parameters={},
                requested_range=disposition._requested_range(),
                expected_media_type="application/json",
                parser_version="v5.2.1",
                parent_query_id=None,
                run_spec_sha256=context.authority_sha256,
                acquisition_contract_sha256=context.acquisition_contract_sha256,
            )
            authority = disposition.DuplicateGamePkDispositionAuthority(
                repository_root=root,
                evidence_root=context.raw_root,
                source_run_spec_path=context.authority_path,
                acquisition_contract_path=context.acquisition_contract_path,
                query=query,
                old_raw_relative=disposition.OLD_RAW_RELATIVE,
                _snapshots=(),
            )
            artifacts = (
                (disposition.OLD_REGISTRATION_RELATIVE, "query_registration"),
                (disposition.OLD_QUARANTINE_RELATIVE, "quarantine_receipt"),
                (disposition.OLD_RAW_RELATIVE, "quarantined_raw"),
            )
            for relative, artifact_kind in artifacts:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "artifact_kind": artifact_kind,
                    "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
                    "fixture_kind": "LOCAL_UNAUTHENTICATED_SCHEDULE_INVENTORY",
                    "relative_path": relative.as_posix(),
                    "source_query_id": disposition.OLD_QUERY_ID,
                }
                contents = _canonical(payload)
                path.write_bytes(contents)
                node = path.lstat()
                if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1:
                    raise AssertionError(
                        f"unsafe local disposition artifact: {relative}"
                    )
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != hashlib.sha256(contents).hexdigest()
                ):
                    raise AssertionError(
                        f"local disposition artifact hash differs: {relative}"
                    )
                observed = json.loads(path.read_bytes())
                if (
                    _canonical(observed) != contents
                    or observed["relative_path"] != relative.as_posix()
                    or observed["source_query_id"] != disposition.OLD_QUERY_ID
                    or observed["disposition"] != "SCHEDULE_DUPLICATE_GAMEPK_565534"
                ):
                    raise AssertionError(
                        f"local disposition artifact binding differs: {relative}"
                    )
            authorities[root] = authority
            return authority

    boundary = Boundary()
    patchers = [
        patch.object(
            disposition,
            "build_duplicate_disposition_authority",
            side_effect=build_fixture_authority,
        ),
        patch.object(
            disposition.DuplicateGamePkDispositionAuthority,
            "recompute",
            side_effect=recompute_fixture_authority,
            autospec=True,
        ),
    ]
    for patcher in patchers:
        patcher.start()
    _ACTIVE_LOCAL_DISPOSITION_BOUNDARY = boundary
    try:
        yield boundary
    finally:
        _ACTIVE_LOCAL_DISPOSITION_BOUNDARY = None
        for patcher in reversed(patchers):
            patcher.stop()


def _source_fixture(base: Path) -> _SourceFixture:
    """Build a local source fixture without copying protected 565534 evidence."""
    from mlb_backtest.v52_duplicate_gamepk_disposition import (
        build_duplicate_disposition_authority,
        publish_duplicate_gamepk_disposition,
    )
    root = (base / "repo").resolve()
    authority_files = tuple(
        dict.fromkeys(
            (
                *_SOURCE_AUTHORITY_FILES,
                Path("schemas/v5_2_run_spec.schema.json"),
                Path("schemas/v5_2_derived_disposition_sidecar.schema.json"),
            )
        )
    )
    for relative in authority_files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for relative in (
        "data/v5_2_qd/historical_superseding_565534/raw",
        "data/v5_2_qd/historical_superseding_565534/sources",
        "audit/v5_2_qd/historical_superseding_565534/acquisition",
        "audit/v5_2_qd/historical_superseding_565534/execution",
        "reports/v5_2_qd/historical_superseding_565534",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "schedule@example.test"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Schedule Test"], cwd=root, check=True
    )
    subprocess.run(["git", "add", *map(str, authority_files)], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "source authority"], cwd=root, check=True)

    source_run_path = root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT)
    source_contract_path = root / SOURCE_CONTRACT_PATH.relative_to(ROOT)
    boundary = _ACTIVE_LOCAL_DISPOSITION_BOUNDARY
    if boundary is None:
        raise RuntimeError("source fixture requires local disposition boundary")
    boundary.install(root, source_run_path, source_contract_path)
    source_run = load_source_run_spec(source_run_path)
    batch = build_static_batch(source_run_path, source_contract_path)
    publish_query_batch(root, batch)
    authority = build_duplicate_disposition_authority(
        root,
        source_run_spec_path=source_run_path,
        acquisition_contract_path=source_contract_path,
    )
    store = V52EvidenceStore(
        root / "data/v5_2_qd/historical_superseding_565534/raw"
    )
    for index, query in enumerate(batch.queries):
        if index == 1:
            publish_duplicate_gamepk_disposition(store, authority)
            continue
        raw = _schedule_raw(query, index)
        sidecar = recompute_schedule_anchor_sidecar(raw, query)
        with store.acquire_query(query) as lease:
            store.publish_fetched(
                query,
                store.next_attempt_number(lease),
                _capture(raw),
                sidecar["semantics"],
            )

    return _SourceFixture(
        root=root,
        evidence_root=store.root,
        run_spec=source_run,
        contract_path=source_contract_path,
        batch_id=batch.batch_id,
    )


def _source_registry_fixture(base: Path) -> _SourceFixture:
    """Build a source fixture with the real append-only execution registry.

    The normal source fixture intentionally stays small for parser/evidence
    tests.  Integration tests that exercise the registry loader need the
    complete bound-code tuple and Task 0 lineage copied into the fixture
    repository before initializing the registry.
    """
    from mlb_backtest.execution_registry import (
        advance_execution_phase,
        initialize_execution_registry,
    )
    from tests.test_execution_registry import (
        BASELINE_RELATIVE,
        BOUND_EXECUTION_CODE,
        BOUND_SCHEMA_PATHS,
        PROTECTED_RELATIVE,
    )

    fixture = _source_fixture(base)
    root = fixture.root
    for relative in (
        BASELINE_RELATIVE,
        PROTECTED_RELATIVE,
        *BOUND_EXECUTION_CODE,
        *BOUND_SCHEMA_PATHS,
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "execution registry authority"],
        cwd=root,
        check=True,
    )
    initialize_execution_registry(
        root,
        fixture.run_spec,
        design_path=root
        / "docs/superpowers/specs/"
        / "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md",
        plan_path=root
        / "docs/superpowers/plans/"
        / "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md",
        acquisition_contract_path=fixture.contract_path,
    )
    advance_execution_phase(
        root,
        fixture.run_spec,
        expected_phase="DESIGN_ACCEPTED",
        next_phase="ACQUIRING",
        inventory_path=root / PROTECTED_RELATIVE,
        validation_receipts=(),
    )
    return fixture


def _memoized_registry_git_patch():
    """Isolate immutable Git plumbing while exercising real registry loaders."""
    import mlb_backtest.execution_registry as execution_registry
    import mlb_backtest.v52_query_plan as query_plan

    original = execution_registry._git
    original_bytes = execution_registry._git_bytes
    original_capture = execution_registry._capture_git_snapshot
    original_head_blob = query_plan._verify_head_blob
    cache: dict[tuple[str, tuple[str, ...]], str] = {}

    def cached(root: Path, *args: str) -> str:
        key = (os.fspath(root), tuple(args))
        if key not in cache:
            cache[key] = original(root, *args)
        return cache[key]

    bytes_cache: dict[tuple[str, tuple[str, ...]], bytes] = {}

    def cached_bytes(root: Path, *args: str) -> bytes:
        key = (os.fspath(root), tuple(args))
        if key not in bytes_cache:
            bytes_cache[key] = original_bytes(root, *args)
        return bytes_cache[key]

    head_cache: dict[str, object] = {}

    def stable_snapshot(root: Path, _evidence: Mapping[str, object]):
        key = os.fspath(root)
        if key not in head_cache:
            head_cache[key] = original_capture(root, _evidence)
        return head_cache[key]

    head_blob_cache: dict[tuple[object, ...], str] = {}

    def cached_head_blob(
        root: Path,
        relative: Path,
        contents: bytes,
        label: str,
        *,
        expected_blob: object | None = None,
    ) -> str:
        key = (
            os.fspath(root),
            relative.as_posix(),
            hashlib.sha256(contents).hexdigest(),
            label,
            expected_blob,
        )
        if key not in head_blob_cache:
            head_blob_cache[key] = original_head_blob(
                root,
                relative,
                contents,
                label,
                expected_blob=expected_blob,
            )
        return head_blob_cache[key]

    stack = ExitStack()
    stack.enter_context(
        patch.object(execution_registry, "_git_bytes", cached_bytes)
    )
    stack.enter_context(patch.object(execution_registry, "_git", cached))
    stack.enter_context(
        patch.object(execution_registry, "_capture_git_snapshot", stable_snapshot)
    )
    stack.enter_context(patch.object(query_plan, "_verify_head_blob", cached_head_blob))
    return stack


def _memoized_disposition_projection_patch():
    """Provide the historical name while scoping the local fixture seam."""
    return _local_disposition_fixture_boundary()


class V52ScheduleInventoryTest(unittest.TestCase):
    @staticmethod
    def _checkpoint(fixture: _SourceFixture, disposition_id: str, *, completed: tuple[str, ...] | None = None):
        return SimpleNamespace(
            query_batch_ids=(fixture.batch_id,),
            dispositioned_query_ids=(disposition_id,),
            completed_query_ids=completed if completed is not None else (disposition_id,),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )

    def test_source_anchor_provenance_binds_exact_boundary_hashes(self) -> None:
        from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_provenance

        fixture = json.loads(NEUTRALIZATION_FIXTURE.read_text())
        anchors = [row["anchor"] for row in fixture["records"]]
        actual = build_schedule_anchor_provenance(anchors)
        self.assertEqual(set(actual), {565338, 565535, 566156, 566157})
        for row in fixture["records"]:
            game_pk = row["anchor"]["game_pk"]
            self.assertEqual(
                actual[game_pk],
                {
                    "game_pk": game_pk,
                    "official_date": row["anchor"]["official_date"],
                    "away_team_id": row["anchor"]["away_team_id"],
                    "home_team_id": row["anchor"]["home_team_id"],
                    "raw_game_object_sha256": row["anchor"]["raw_game_object_sha256"],
                    "normalized_anchor_sha256": row["normalized_anchor_sha256"],
                },
            )

    def test_source_anchor_provenance_rejects_closed_shape_attacks(self) -> None:
        from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_provenance

        fixture = json.loads(NEUTRALIZATION_FIXTURE.read_text())
        anchor = fixture["records"][0]["anchor"]
        attacks = (
            {
                key: value
                for key, value in anchor.items()
                if key != "raw_game_object_sha256"
            },
            {**anchor, "unexpected": True},
            {
                **anchor,
                "raw_game_object_sha256": anchor["raw_game_object_sha256"][:32],
            },
        )
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                build_schedule_anchor_provenance([attack])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_schedule_anchor_provenance([anchor, anchor])

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
            (root / SOURCE_ADDENDUM_PATH).write_text("tampered\n", encoding="utf-8")
            source_raw = (
                root / "data/v5_2_qd/historical_superseding_565534/raw"
            )
            with (
                patch(
                    "mlb_backtest.v52_schedule_inventory.load_query_batch",
                    side_effect=AssertionError("query-batch I/O reached"),
                ) as loader,
                self.assertRaisesRegex(ValueError, "addendum.*hash|binding"),
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
                self.assertRaisesRegex(ValueError, "addendum.*hash|binding"),
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
                self.assertRaisesRegex(ValueError, "addendum.*hash|binding"),
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

    def test_schedule_inventory_contains_one_565534_and_only_retained_children(self) -> None:
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            EXPECTED_OCCURRENCE_COUNTS,
            RETAINED_OBJECT_SHA256,
            SUPPRESSED_OBJECT_SHA256,
        )
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_query_plan import build_static_batch
        from mlb_backtest.v52_query_plan import (
            _build_schedule_reference_batch_from_inventory,
        )
        from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_inventory

        with tempfile.TemporaryDirectory() as td, _local_disposition_fixture_boundary():
            fixture = _source_fixture(Path(td).resolve())
            disposition_id = logical_query_id(
                build_static_batch(
                    fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
                    fixture.contract_path,
                ).queries[1]
            )
            checkpoint = self._checkpoint(fixture, disposition_id)
            with (
                patch(
                    "mlb_backtest.execution_registry.load_execution_registry",
                    return_value=SimpleNamespace(phase="ACQUIRING"),
                ),
                patch(
                    "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                    return_value=checkpoint,
                ),
            ):
                payload = build_schedule_anchor_inventory(
                    fixture.root,
                    fixture.run_spec,
                    acquisition_contract_path=fixture.contract_path,
                    static_batch_id=fixture.batch_id,
                    evidence_root=fixture.evidence_root,
                )

            self.assertEqual(payload["run_id"], fixture.run_spec.run_id)
            self.assertEqual(
                [row["season"] for row in payload["season_sources"]],
                list(range(2018, 2025)),
            )
            source_2019 = payload["season_sources"][1]
            self.assertEqual(source_2019["storage_mode"], "DERIVED_DISPOSITION")
            self.assertIsNone(source_2019["raw_sha256"])
            from mlb_backtest import v52_duplicate_gamepk_disposition as disposition

            authority = disposition.build_duplicate_disposition_authority(
                fixture.root,
                source_run_spec_path=fixture.root
                / SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
                acquisition_contract_path=fixture.contract_path,
            )
            self.assertEqual(
                source_2019["query_id"], logical_query_id(authority.query)
            )
            for binding_name in ("query_sidecar", "success_receipt"):
                binding = source_2019[binding_name]
                self.assertEqual(set(binding), {"path", "sha256"})
                self.assertTrue(binding["path"])
                self.assertEqual(len(binding["sha256"]), 64)
            self.assertEqual(
                source_2019["occurrence_counts"], EXPECTED_OCCURRENCE_COUNTS
            )
            self.assertEqual(
                payload["static_batch"]["batch_id"], fixture.batch_id
            )
            anchors = payload["anchors"]
            retained = [anchor for anchor in anchors if anchor["game_pk"] == 565534]
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0]["venue_id"], 10)
            self.assertEqual(
                retained[0]["scheduled_start_utc"], "2019-09-07T00:15:00Z"
            )
            self.assertEqual(
                retained[0]["raw_game_object_sha256"], RETAINED_OBJECT_SHA256
            )
            self.assertFalse(
                any(
                    anchor.get("raw_game_object_sha256") == SUPPRESSED_OBJECT_SHA256
                    for anchor in anchors
                )
            )
            self.assertTrue(
                all(
                    isinstance(anchor.get("raw_game_object_sha256"), str)
                    and len(anchor["raw_game_object_sha256"]) == 64
                    for anchor in anchors
                )
            )

            inventory_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
            run_sha256 = payload["run_spec"]["sha256"]
            contract_sha256 = payload["acquisition_contract"]["sha256"]
            reference = _build_schedule_reference_batch_from_inventory(
                payload,
                inventory_sha256=inventory_sha256,
                run_spec_sha256=run_sha256,
                acquisition_contract_sha256=contract_sha256,
            )
            boxscores = [
                query
                for query in reference.queries
                if query.requested_range.get("game_pk") == 565534
            ]
            self.assertEqual(len(boxscores), 1)
            consumers = [
                query
                for query in reference.queries
                if 565534 in query.requested_range.get("consuming_game_pks", [])
            ]
            self.assertGreaterEqual(len(consumers), 1)
            self.assertTrue(
                all(
                    query.requested_range["consuming_game_pks"].count(565534) == 1
                    for query in consumers
                )
            )

    def test_source_inventory_requires_authenticated_task6_checkpoint_population(self) -> None:
        import mlb_backtest.v52_schedule_inventory as inventory
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_query_plan import build_static_batch

        with tempfile.TemporaryDirectory() as td, _local_disposition_fixture_boundary():
            fixture = _source_fixture(Path(td).resolve())
            disposition_id = logical_query_id(
                build_static_batch(
                    fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
                    fixture.contract_path,
                ).queries[1]
            )
            for failure in (
                "missing checkpoint",
                "wrong disposition population",
                "derived query not completed",
                "checkpoint changed after first read",
            ):
                with self.subTest(failure=failure):
                    if failure == "missing checkpoint":
                        checkpoint = None
                    elif failure == "wrong disposition population":
                        checkpoint = self._checkpoint(fixture, "0" * 64)
                    elif failure == "derived query not completed":
                        checkpoint = self._checkpoint(fixture, disposition_id, completed=())
                    else:
                        checkpoint = self._checkpoint(fixture, disposition_id)
                    values = [checkpoint]
                    if failure == "missing checkpoint":
                        values.append(None)
                    if failure == "checkpoint changed after first read":
                        values.append(self._checkpoint(fixture, "1" * 64))
                    with patch.object(
                        inventory,
                        "load_source_verification_checkpoint",
                        side_effect=values,
                        create=True,
                    ), patch(
                        "mlb_backtest.execution_registry.load_execution_registry",
                        return_value=SimpleNamespace(phase="ACQUIRING"),
                    ), patch(
                        "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                        side_effect=values,
                    ):
                        with self.assertRaises(ValueError):
                            inventory.build_schedule_anchor_inventory(
                                fixture.root,
                                fixture.run_spec,
                                acquisition_contract_path=fixture.contract_path,
                                static_batch_id=fixture.batch_id,
                                evidence_root=fixture.evidence_root,
                            )

    def test_source_inventory_uses_real_registry_checkpoint_and_fails_closed_on_mutation(self) -> None:
        """The schedule gate must consume the append-only registry, not a mock."""
        from mlb_backtest.execution_registry import (
            load_execution_registry,
            load_latest_acquisition_checkpoint,
            publish_acquisition_checkpoint,
        )
        from mlb_backtest.v52_query_plan import build_static_batch
        from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_inventory

        with (
            _memoized_registry_git_patch(),
            _memoized_disposition_projection_patch(),
            tempfile.TemporaryDirectory() as td,
        ):
            fixture = _source_registry_fixture(Path(td).resolve())
            disposition_query_id = logical_query_id(
                build_static_batch(
                    fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
                    fixture.contract_path,
                ).queries[1]
            )
            completed_query_ids = tuple(
                sorted(
                    logical_query_id(query)
                    for query in build_static_batch(
                        fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(ROOT),
                        fixture.contract_path,
                    ).queries
                )
            )
            checkpoint = publish_acquisition_checkpoint(
                fixture.root,
                fixture.run_spec,
                expected_prior_checkpoint_sha256=None,
                stage="STATIC_SCHEDULES",
                query_batch_ids=(fixture.batch_id,),
                completed_query_ids=completed_query_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                dispositioned_query_ids=(disposition_query_id,),
            )
            self.assertEqual(
                load_execution_registry(fixture.root, fixture.run_spec).phase,
                "ACQUIRING",
            )
            self.assertEqual(
                load_latest_acquisition_checkpoint(fixture.root, fixture.run_spec),
                checkpoint,
            )
            payload = build_schedule_anchor_inventory(
                fixture.root,
                fixture.run_spec,
                acquisition_contract_path=fixture.contract_path,
                static_batch_id=fixture.batch_id,
                evidence_root=fixture.evidence_root,
            )
            self.assertEqual(payload["run_id"], fixture.run_spec.run_id)

            canonical_checkpoint_path = (
                fixture.root
                / "audit/v5_2_qd/historical_superseding_565534/execution/"
                / "acquisition_checkpoints"
                / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
            )
            original = canonical_checkpoint_path.read_bytes()
            original_payload = json.loads(original)
            for mutation in (
                "missing",
                "wrong disposition",
                "incomplete disposition",
                "final bytes",
            ):
                with self.subTest(mutation=mutation):
                    for candidate in canonical_checkpoint_path.parent.iterdir():
                        if candidate.name.startswith("00000000-"):
                            candidate.unlink()
                    checkpoint_path = canonical_checkpoint_path
                    checkpoint_path.write_bytes(original)
                    if mutation == "missing":
                        checkpoint_path.unlink()
                    elif mutation in {
                        "wrong disposition",
                        "incomplete disposition",
                    }:
                        forged = copy.deepcopy(original_payload)
                        if mutation == "wrong disposition":
                            forged["dispositioned_query_ids"] = ["0" * 64]
                        else:
                            forged["completed_query_ids"].remove(
                                disposition_query_id
                            )
                            forged["pending_query_ids"] = [
                                disposition_query_id
                            ]
                        forged_bytes = _canonical(forged)
                        forged_digest = hashlib.sha256(forged_bytes).hexdigest()
                        checkpoint_path.unlink()
                        checkpoint_path = checkpoint_path.with_name(
                            f"{checkpoint.sequence:08d}-{forged_digest}.json"
                        )
                        checkpoint_path.write_bytes(forged_bytes)
                    else:
                        checkpoint_path.write_bytes(original + b" ")
                    with (
                        patch.object(
                            V52EvidenceStore,
                            "read_verified_query_progress",
                            side_effect=AssertionError(
                                "checkpoint disposition reached evidence read"
                            ),
                        ),
                        patch.object(
                            V52EvidenceStore,
                            "read_verified_success",
                            side_effect=AssertionError(
                                "checkpoint disposition reached inventory evidence"
                            ),
                        ),
                        patch(
                            "mlb_backtest.v52_schedule_inventory._read_inventory_contents",
                            side_effect=AssertionError(
                                "checkpoint disposition reached inventory read"
                            ),
                        ),
                        self.assertRaises((OSError, RuntimeError, ValueError)),
                    ):
                        if mutation != "final bytes":
                            build_schedule_anchor_inventory(
                                fixture.root,
                                fixture.run_spec,
                                acquisition_contract_path=fixture.contract_path,
                                static_batch_id=fixture.batch_id,
                                evidence_root=fixture.evidence_root,
                            )
                        else:
                            load_latest_acquisition_checkpoint(
                                fixture.root, fixture.run_spec
                            )

    def test_source_checkpoint_context_rejects_raw_mapping_and_old_run_authority(self) -> None:
        import mlb_backtest.execution_registry as registry
        import mlb_backtest.v52_schedule_inventory as inventory

        raw_checkpoint = {
            "run_id": "historical_walkforward_500_superseding_565534",
            "stage": "STATIC_SCHEDULES",
            "query_batches": [
                {
                    "batch_id": "1" * 64,
                    "stage": "STATIC_SCHEDULES",
                }
            ],
            "completed_query_ids": ["2" * 64],
            "pending_query_ids": [],
            "quarantined_query_ids": [],
            "split_query_ids": [],
            "dispositioned_query_ids": ["2" * 64],
        }
        self.assertFalse(
            hasattr(inventory, "_using_source_checkpoint_authority")
        )
        self.assertFalse(
            hasattr(inventory, "_issue_source_checkpoint_authority")
        )
        with self.assertRaises((AttributeError, TypeError, ValueError)):
            with registry._using_source_checkpoint_authority(raw_checkpoint):
                pass

        source_run = load_source_run_spec(SOURCE_RUN_SPEC_PATH)
        old_run = load_run_spec(RUN_SPEC_PATH)
        for run_spec in (source_run, old_run):
            with self.subTest(run_id=run_spec.run_id), self.assertRaises(
                (AttributeError, TypeError, ValueError)
            ):
                registry._issue_source_checkpoint_authority(
                    raw_checkpoint,
                    run_spec,
                    object(),
                )

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

    def test_schema_has_exact_old_and_source_schedule_inventory_branches(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_bytes())
        branches = schema.get("oneOf")
        self.assertIsInstance(branches, list)
        by_run = {
            branch["properties"]["run_id"]["const"]: branch
            for branch in branches
            if isinstance(branch, dict)
            and isinstance(branch.get("properties"), dict)
            and isinstance(branch["properties"].get("run_id"), dict)
            and "const" in branch["properties"]["run_id"]
        }
        self.assertEqual(
            set(by_run),
            {
                "historical_walkforward_500",
                "historical_walkforward_500_superseding_565534",
                "historical_walkforward_500_recovery_schedule_conflicts",
            },
        )
        old_branch = by_run["historical_walkforward_500"]
        source_branch = by_run["historical_walkforward_500_superseding_565534"]
        old_defs = old_branch.get("$defs", schema.get("$defs", {}))
        source_defs = source_branch.get("$defs", schema.get("$defs", {}))
        old_anchor = old_defs["anchor"]
        source_anchor = source_defs["anchor"]
        self.assertNotIn("raw_game_object_sha256", old_anchor["required"])
        self.assertIn("raw_game_object_sha256", source_anchor["required"])
        source_season = source_defs["seasonSource"]
        self.assertIn("occurrence_counts", source_season["properties"])
        self.assertEqual(
            source_season["properties"]["storage_mode"]["enum"],
            ["FETCHED_RAW", "DERIVED_DISPOSITION"],
        )
        recovery_branch = by_run[
            "historical_walkforward_500_recovery_schedule_conflicts"
        ]
        self.assertEqual(
            recovery_branch["required"], ["schedule_conflict_dispositions"]
        )

class RecoveryDispositionBindingsTests(unittest.TestCase):
    def _published_recovery_schedule_fixture(self, directory: Path):
        from tests.v52_recovery_schedule_conflict_fixture import (
            recovery_authority_fixture,
        )
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
        )
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        root, run_path, contract_path = recovery_authority_fixture(directory)
        run_spec = load_source_run_spec(run_path)
        batch = build_static_batch(run_path, contract_path)
        publish_query_batch(root, batch)
        context = _load_static_contracts(run_path, contract_path)
        context.raw_root.mkdir(parents=True)
        store = V52EvidenceStore(context.raw_root)
        catalog = build_recovery_schedule_conflict_catalog(
            root, run_path, contract_path
        )
        by_query = {
            logical_query_id(authority.query): authority
            for authority in catalog.entries
        }
        for index, query in enumerate(batch.queries):
            authority = by_query.get(logical_query_id(query))
            if authority is not None:
                with store.acquire_query(query):
                    store.publish_derived_disposition(
                        query,
                        recompute_query_sidecar(query, b"", authority=authority),
                        authority=authority,
                    )
                continue
            raw = _schedule_raw(query, index)
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(raw),
                    recompute_query_sidecar(query, raw, context=context),
                )
        return root, run_spec, contract_path, batch.batch_id, context.raw_root

    def test_recovery_schedule_publish_then_verified_load_rebinds_all_bindings(
        self,
    ) -> None:
        from mlb_backtest.v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
            publish_schedule_anchor_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec, contract, batch_id, evidence = (
                self._published_recovery_schedule_fixture(Path(directory))
            )
            published = publish_schedule_anchor_inventory(
                root,
                run_spec,
                acquisition_contract_path=contract,
                static_batch_id=batch_id,
                evidence_root=evidence,
            )
            loaded = load_verified_schedule_anchor_inventory(
                root,
                run_spec,
                acquisition_contract_path=contract,
                static_batch_id=batch_id,
                evidence_root=evidence,
            )
            self.assertEqual(loaded, published)
            catalog = loaded.payload["schedule_conflict_dispositions"]
            self.assertEqual(len(catalog["entries"]), 3)
            self.assertEqual(catalog["query_ids"], sorted(catalog["query_ids"]))

    def test_verified_recovery_load_passes_fresh_published_bindings_to_validation(
        self,
    ) -> None:
        from mlb_backtest import v52_schedule_inventory as inventory

        context = SimpleNamespace(
            acquisition_audit_root=Path("/repo/audit"),
            acquisition_contract_path=Path("/repo/contract.json"),
            authority_path=Path("/repo/run.json"),
            run_id="historical_walkforward_500_recovery_schedule_conflicts",
        )
        bindings = object()
        payload = {"schedule_conflict_dispositions": {"entries": [], "query_ids": []}}
        with (
            patch.object(inventory, "_absolute", return_value=Path("/repo")),
            patch.object(inventory, "_authenticate", return_value=context),
            patch.object(inventory, "_read_inventory_contents", return_value=b"{}\n"),
            patch.object(inventory.json, "loads", return_value=payload),
            patch.object(
                inventory,
                "_canonical_file_bytes",
                return_value=b"{}\n",
            ),
            patch(
                "mlb_backtest.v52_schedule_conflict_recovery_disposition."
                "build_recovery_schedule_conflict_catalog",
                return_value=object(),
            ),
            patch(
                "mlb_backtest.v52_schedule_conflict_recovery_disposition."
                "load_recovery_disposition_bindings",
                return_value=bindings,
            ),
            patch.object(
                inventory, "_validate_payload", return_value=payload
            ) as validate,
            patch.object(
                inventory, "build_schedule_anchor_inventory", return_value=payload
            ),
            patch.object(inventory, "_deep_freeze", return_value=payload),
            patch.object(
                inventory.RecoveryDispositionBindings,
                "from_catalog_mapping",
                return_value=object(),
            ),
        ):
            inventory.load_verified_schedule_anchor_inventory(
                Path("/repo"),
                object(),
                acquisition_contract_path=Path("/repo/contract.json"),
                static_batch_id="0" * 64,
                evidence_root=Path("/repo/raw"),
            )
        self.assertIs(validate.call_args.kwargs["recovery_bindings"], bindings)

    def test_recovery_projection_rebind_rejects_catalog_a_raw_b_restore_a_race(
        self,
    ) -> None:
        """The initial projection binds raw B; the final catalog sees raw A."""
        from mlb_backtest import v52_schedule_inventory as inventory

        query = SimpleNamespace(
            query_id="recovery-query", requested_range={"year": "2019"}
        )
        authority = SimpleNamespace(query=query)
        catalog = SimpleNamespace(entries=(authority,))
        context = SimpleNamespace(
            authority_path=Path("/repo/run.json"),
            acquisition_contract_path=Path("/repo/contract.json"),
        )
        raw_b_projection = ([{"game_pk": 565534, "raw": "B"}], {"565534": 1})
        raw_a_projection = ([{"game_pk": 565534, "raw": "A"}], {"565534": 1})
        with (
            patch.object(
                inventory,
                "logical_query_id",
                side_effect=lambda item: item.query_id,
            ),
            patch(
                "mlb_backtest.v52_schedule_conflict_recovery_disposition."
                "build_recovery_schedule_conflict_catalog",
                return_value=catalog,
            ),
            patch(
                "mlb_backtest.v52_schedule_conflict_recovery_disposition."
                "load_recovery_disposition_bindings",
                return_value=(),
            ),
            patch.object(
                inventory,
                "_recovery_projection_anchors",
                return_value=raw_a_projection,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "recovery disposition projection changed before return"
            ):
                inventory._rebind_recovery_schedule_projections(
                    Path("/repo"),
                    context,
                    {"recovery-query": raw_b_projection},
                    (),
                    evidence_relative=Path("data/raw"),
                    store=object(),
                )

    def test_only_catalog_factory_can_authenticate_recovery_bindings(self) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionBinding,
        )
        from mlb_backtest.v52_schedule_inventory import RecoveryDispositionBindings

        def binding(game_pk: int, query_id: str) -> RecoveryDispositionBinding:
            return RecoveryDispositionBinding(
                game_pk=game_pk,
                query_id=query_id,
                query_sidecar={"path": f"sidecars/{query_id}.json", "sha256": "a" * 64},
                success_receipt={"path": f"queries/{query_id}/success.json", "sha256": "b" * 64},
                retained_scheduled_start_utc="2020-08-14T21:05:00Z",
                played_date=None,
                result_available_on=None,
                raw_object_sha256s=("c" * 64,),
                selected_raw_game_object_sha256s_sha256="d" * 64,
                normalized_games_sha256="e" * 64,
                sidecar_semantics_sha256="f" * 64,
                expected_derived_sidecar_sha256="0" * 64,
            )

        entries = (
            binding(565534, "b" * 64),
            binding(630882, "a" * 64),
            binding(633224, "c" * 64),
        )
        direct = RecoveryDispositionBindings(entries)
        mapping = direct.as_mapping()
        self.assertEqual(mapping["query_ids"], ["a" * 64, "b" * 64, "c" * 64])
        with self.assertRaisesRegex(ValueError, "authority differs"):
            direct.reauthenticate()
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(mapping, entries)
        with self.assertRaises((TypeError, ValueError)):
            RecoveryDispositionBindings(
                entries,
                _authenticated=True,
                _reauthenticate=lambda: entries,
            )
        reordered = copy.deepcopy(mapping)
        reordered["entries"].reverse()
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(reordered, entries)
        missing = copy.deepcopy(mapping)
        del missing["entries"]
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(missing, entries)
        extra = copy.deepcopy(mapping)
        extra["extra"] = True
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(extra, entries)
        copied = copy.deepcopy(mapping)
        copied["entries"][1]["success_receipt"] = copied["entries"][0][
            "success_receipt"
        ]
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(copied, entries)
        cross_run = copy.deepcopy(mapping)
        cross_run["entries"][0]["query_sidecar"]["path"] = (
            "other-run/sidecar.json"
        )
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(cross_run, entries)
        stale = copy.deepcopy(mapping)
        stale["entries"][2]["sidecar_semantics_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "authority differs"):
            RecoveryDispositionBindings.from_mapping(stale, entries)


if __name__ == "__main__":
    unittest.main()
