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
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    logical_query_id,
)
from mlb_backtest.v52_source_contracts import recompute_query_sidecar
from mlb_backtest.source_parsers.schedule import parse_schedule
import mlb_backtest.v52_duplicate_gamepk_disposition as disposition_module
import mlb_backtest.v52_evidence as evidence_module
from mlb_backtest.v52_duplicate_gamepk_disposition import (
    DISPOSITION_ENDPOINT,
    EXPECTED_OCCURRENCE_COUNTS,
    NORMALIZED_GAMES_SHA256,
    SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256,
    build_duplicate_disposition_authority as _build_duplicate_disposition_authority,
    publish_duplicate_gamepk_disposition,
    project_schedule_occurrences,
)
from tests.v52_recovery_schedule_conflict_fixture import (
    PARENT_RELATIVES,
    matching_source_authority_root,
    parent_tree_fingerprints,
    recovery_authority_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIGURED_SOURCE_AUTHORITY_ROOT = Path(
    os.environ.get("MLB_V52_SOURCE_AUTHORITY_ROOT", str(ROOT))
).resolve()
SOURCE_AUTHORITY_ROOT = matching_source_authority_root(
    ROOT, CONFIGURED_SOURCE_AUTHORITY_ROOT
)
RECOVERY_PARENT_EVIDENCE_ROOT = Path(
    os.environ.get(
        "MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT",
        str(CONFIGURED_SOURCE_AUTHORITY_ROOT),
    )
).resolve()
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"
DISPOSITION_FIXTURES = ROOT / "tests/fixtures/v52_duplicate_gamepk_565534"
_SCHEDULE_FEED_EVIDENCE_PATTERNS = (
    "data/v5_2_qd/historical/**/mlb_schedule_game_feed/**/*",
    "data/v5_2_qd/historical_superseding_565534/**/mlb_schedule_game_feed/**/*",
)


def _schedule_feed_evidence_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {
                path
                for pattern in _SCHEDULE_FEED_EVIDENCE_PATTERNS
                for path in root.glob(pattern)
                if path.is_file()
            }
        )
    )


def _current_tree_checkout_with_missing_parent_blob(base: Path) -> tuple[Path, str, str]:
    """Build a shallow checkout whose tip tree is complete but parent history is broken."""
    from tests.v52_recovery_schedule_conflict_fixture import _git_run

    checkout = (Path(tempfile.mkdtemp(prefix="v52-missing-parent-")) / "checkout").resolve()
    checkout.parent.mkdir(parents=True, exist_ok=True)
    home = checkout.parent / "git-home"
    _git_run(
        [
            "clone",
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--single-branch",
            "--depth=1",
            "-q",
            base.resolve().as_uri(),
            str(checkout),
        ],
        cwd=checkout.parent,
        home=home,
    )
    _git_run(
        ["checkout", "-qb", "fixture-source"], cwd=checkout, home=home
    )
    original_tree = _git_run(
        ["rev-parse", "HEAD^{tree}"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    _git_run(
        ["config", "--local", "user.email", "v52-fixture@example.test"],
        cwd=checkout,
        home=home,
    )
    _git_run(
        ["config", "--local", "user.name", "V52 Fixture"],
        cwd=checkout,
        home=home,
    )
    _git_run(["config", "--local", "gc.auto", "0"], cwd=checkout, home=home)
    historical = checkout / "historical-only.txt"
    historical.write_bytes(b"parent-only\n")
    _git_run(["add", "historical-only.txt"], cwd=checkout, home=home)
    _git_run(
        ["commit", "-qm", "fixture parent A"], cwd=checkout, home=home
    )
    blob = _git_run(
        ["rev-parse", "HEAD:historical-only.txt"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    parent_tree = _git_run(
        ["rev-parse", "HEAD^{tree}"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    historical.unlink()
    _git_run(["add", "-u"], cwd=checkout, home=home)
    _git_run(["commit", "-qm", "fixture tip B"], cwd=checkout, home=home)
    head = _git_run(
        ["rev-parse", "HEAD"], cwd=checkout, home=home, text=True, capture_output=True
    ).stdout.strip()
    _git_run(["tag", "fixture-intended-tip", head], cwd=checkout, home=home)
    unrelated_head = _git_run(
        ["commit-tree", parent_tree, "-m", "fixture unrelated root"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    _git_run(["branch", "fixture-unrelated", unrelated_head], cwd=checkout, home=home)
    tag_head = _git_run(
        ["rev-parse", "refs/tags/fixture-intended-tip"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    unrelated_ref = _git_run(
        ["rev-parse", "refs/heads/fixture-unrelated"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    intended_tree = _git_run(
        ["rev-parse", f"{head}^{{tree}}"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    unrelated_tree = _git_run(
        ["rev-parse", "refs/heads/fixture-unrelated^{tree}"],
        cwd=checkout,
        home=home,
        text=True,
        capture_output=True,
    ).stdout.strip()
    merge_base = subprocess.run(
        ["git", "merge-base", head, "refs/heads/fixture-unrelated"],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if (
        tag_head != head
        or intended_tree != original_tree
        or unrelated_ref != unrelated_head
        or unrelated_ref == head
        or unrelated_tree == intended_tree
        or merge_base.returncode == 0
    ):
        raise AssertionError("fixture refs do not isolate intended current tip")
    _git_run(["checkout", "fixture-source"], cwd=checkout, home=home)
    if (
        _git_run(
            ["rev-parse", "HEAD"],
            cwd=checkout,
            home=home,
            text=True,
            capture_output=True,
        ).stdout.strip()
        != head
    ):
        raise AssertionError("fixture source HEAD was not restored to intended tip")
    loose_blob = checkout / ".git" / "objects" / blob[:2] / blob[2:]
    if not loose_blob.is_file():
        raise AssertionError("historical fixture blob was not loose")
    loose_blob.unlink()
    return checkout, head, blob


def build_duplicate_disposition_authority(
    root: Path,
    *args: object,
    source_run_spec_path: Path,
    acquisition_contract_path: Path,
    **kwargs: object,
) -> object:
    """Route only the 565534 authority to the optional test evidence root."""
    if root == ROOT:
        root = SOURCE_AUTHORITY_ROOT
        source_run_spec_path = SOURCE_AUTHORITY_ROOT / source_run_spec_path.relative_to(ROOT)
        acquisition_contract_path = SOURCE_AUTHORITY_ROOT / acquisition_contract_path.relative_to(ROOT)
    return _build_duplicate_disposition_authority(
        root,
        *args,
        source_run_spec_path=source_run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        **kwargs,
    )


def _query() -> LogicalQuery:
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


def _schedule_payload() -> dict[str, object]:
    payload = json.loads((FIXTURES / "schedule_mini.json").read_bytes())
    payload["totalGames"] = 2
    payload["dates"][0]["totalGames"] = 2
    payload["dates"][0]["games"][0]["neutralSite"] = False
    payload["dates"][0]["games"][0]["isAlternateSite"] = False
    payload["dates"][0]["games"][1]["neutralSite"] = True
    payload["dates"][0]["games"][1]["isAlternateSite"] = True
    return payload


def _schedule_raw() -> bytes:
    return json.dumps(_schedule_payload(), separators=(",", ":"), sort_keys=True).encode()


def _box_query() -> LogicalQuery:
    return LogicalQuery(
        source="mlb_schedule_game_feed", grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
        parameters={}, requested_range={"season": 2024, "game_pk": 9001, "official_date": "2024-04-02"},
        expected_media_type="application/json", parser_version="v5.2.1",
        parent_query_id=None, run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _boxscore_raw() -> bytes:
    payload = json.loads((FIXTURES / "boxscore_9001.json").read_bytes())
    payload["teams"]["away"]["teamStats"] = {"batting": {"runs": 2}}
    payload["teams"]["home"]["teamStats"] = {"batting": {"runs": 4}}
    payload["teams"]["away"]["players"]["ID1011"] = {
        "person": {"id": 1011, "fullName": "Away Substitute"},
        "battingOrder": "101",
    }
    payload["teams"]["home"]["players"]["ID2011"] = {
        "person": {"id": 2011, "fullName": "Home Substitute"},
        "battingOrder": "401",
    }
    payload["teams"]["home"]["players"]["ID2012"] = {
        "person": {"id": 2012, "fullName": "Home Substitute 2"},
        "battingOrder": "902",
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _artifact(sidecar: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(sidecar, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    return raw, hashlib.sha256(raw).hexdigest()


def _test_schedule_provenance(
    anchor_sidecar: Mapping[str, object],
) -> dict[int, dict[str, object]]:
    semantics = anchor_sidecar["semantics"]
    assert isinstance(semantics, Mapping)
    anchors = semantics["anchors"]
    assert isinstance(anchors, list)
    return {
        int(anchor["game_pk"]): {
            "game_pk": anchor["game_pk"],
            "official_date": anchor["official_date"],
            "away_team_id": anchor["away_team_id"],
            "home_team_id": anchor["home_team_id"],
            "raw_game_object_sha256": hashlib.sha256(
                f"raw:{anchor['game_pk']}".encode()
            ).hexdigest(),
            "normalized_anchor_sha256": hashlib.sha256(
                f"normalized:{anchor['game_pk']}".encode()
            ).hexdigest(),
        }
        for anchor in anchors
        if isinstance(anchor, Mapping)
    }


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _assert_contains_no_bytes(test: unittest.TestCase, value: object) -> None:
    if isinstance(value, bytes):
        test.fail("authority or projection retained ephemeral schedule bytes")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_contains_no_bytes(test, item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_contains_no_bytes(test, item)
    elif hasattr(value, "__dict__"):
        _assert_contains_no_bytes(test, vars(value))


def _authenticated_filtered_view(payload: dict[str, object]) -> dict[str, object]:
    filtered = copy.deepcopy(payload)
    date = filtered["dates"][54]
    del date["games"][5]
    date["totalGames"] -= 1
    filtered["totalGames"] -= 1
    return filtered


def _superseding_output_namespace_paths() -> tuple[Path, ...]:
    return tuple(
        SOURCE_AUTHORITY_ROOT / relative
        for relative in (
            "data/v5_2_qd/historical_superseding_565534/sources",
            "reports/v5_2_qd/historical_superseding_565534",
        )
    )


class _RecordingDispositionStore(V52EvidenceStore):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.publish_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def publish_derived_disposition(
        self, *args: object, **kwargs: object
    ) -> object:
        self.publish_calls.append((args, kwargs))
        raise AssertionError("parser mismatch must prevent publication")


def _mutate_565534_predicate(
    payload: dict[str, object], *, retained: bool, field: str
) -> None:
    date_index, game_index = (161, 12) if retained else (54, 5)
    game = payload["dates"][date_index]["games"][game_index]
    if field == "gamePk":
        game["gamePk"] = 565535
    elif field == "gameType":
        game["gameType"] = "S"
    elif field == "season":
        game["season"] = "2020"
    elif field == "officialDate":
        game["officialDate"] = "2019-05-20"
    elif field == "away_team_id":
        game["teams"]["away"]["team"]["id"] = 1
    elif field == "home_team_id":
        game["teams"]["home"]["team"]["id"] = 1
    elif field == "terminal_status":
        game["status"]["detailedState"] = "Scheduled"
    elif field == "away_score":
        game["teams"]["away"]["score"] = 6
    elif field == "home_score":
        game["teams"]["home"]["score"] = 4
    elif field == "winner_team_id":
        game["teams"]["away"]["isWinner"] = False
        game["teams"]["home"]["isWinner"] = True
    elif field == "gameDate":
        game["gameDate"] = "2019-09-07T00:16:00Z"
    elif field == "resumeDate":
        game["resumeDate"] = "2019-09-07T00:16:00Z"
    elif field == "resumeGameDate":
        game["resumeGameDate"] = "2019-09-07"
    elif field == "resumedFrom":
        game["resumedFrom"] = "2019-05-19T17:11:00Z"
    elif field == "resumedFromDate":
        game["resumedFromDate"] = "2019-05-20"
    elif field == "venue_id":
        game["venue"]["id"] = 1
    elif field == "calendar_event_id":
        game["calendarEventID"] = "wrong"
    elif field == "sha256":
        game["description"] = "tampered"
    else:
        raise AssertionError(f"unknown 565534 fixture predicate {field}")


class V52ScheduleSourceTest(unittest.TestCase):
    def test_recovery_schedule_authority_requires_an_exact_static_query_row(
        self,
    ) -> None:
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_query_plan import (
            RECOVERY_STATIC_QUERY_TABLE,
            SourceAuthorityContext,
            _RECOVERY_ACQUISITION_CONTRACT_RELATIVE,
            _RECOVERY_RUN_SPEC_RELATIVE,
            _require_recovery_schedule_query,
        )
        from mlb_backtest.v52_seasons import SEASON_BOUNDARIES

        root = ROOT
        run_path = root / _RECOVERY_RUN_SPEC_RELATIVE
        contract_path = root / _RECOVERY_ACQUISITION_CONTRACT_RELATIVE
        authority = load_source_run_spec(run_path)
        context = SourceAuthorityContext(
            repository_root=root,
            authority=authority,
            authority_path=run_path,
            authority_sha256="a" * 64,
            acquisition_contract_path=contract_path,
            acquisition_contract_sha256="b" * 64,
            contract={},
            run_id=authority.run_id,
            raw_root=root / "data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw",
            sources_root=root / "data/v5_2_qd/historical_recovery_schedule_conflicts_r2/sources",
            acquisition_audit_root=root / "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition",
            execution_audit_root=root / "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/execution",
            report_root=root / "reports/v5_2_qd/historical_recovery_schedule_conflicts_r2",
        )
        year, endpoint, parser_version = RECOVERY_STATIC_QUERY_TABLE[1]
        first, last = SEASON_BOUNDARIES[year]
        query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=endpoint,
            parameters={},
            requested_range={
                "year": year,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
            },
            expected_media_type="application/json",
            parser_version=parser_version,
            parent_query_id=None,
            run_spec_sha256=context.authority_sha256,
            acquisition_contract_sha256=context.acquisition_contract_sha256,
        )

        _require_recovery_schedule_query(query, context)
        for field, value in (
            ("source", "foreign_schedule_source"),
            ("grain", "foreign_schedule_grain"),
            ("expected_media_type", "text/plain"),
            ("parent_query_id", "f" * 64),
            (
                "requested_range",
                {
                    "year": year,
                    "start_date": (first.replace(day=first.day + 1)).isoformat(),
                    "end_date": last.isoformat(),
                },
            ),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "recovery schedule query authority differs"
            ):
                _require_recovery_schedule_query(
                    dataclasses.replace(query, **{field: value}), context
                )
        http_parameters = {
            "sportId": 1,
            "gameType": "R",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "hydrate": "probablePitcher,lineups,venue,linescore",
        }
        _require_recovery_schedule_query(
            dataclasses.replace(
                query,
                endpoint="https://statsapi.mlb.com/api/v1/schedule",
                parameters=http_parameters,
            ),
            context,
        )

    def test_v522_preserves_raw_occurrences_before_normalized_selection(self) -> None:
        """Changing raw duplicate accounting must not change normalized selection."""
        from collections import Counter

        from mlb_backtest.source_parsers.schedule import (
            parse_schedule,
            parse_schedule_occurrences,
        )

        raw_paths = {
            2018: Path(
                "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/"
                "93163d5f3fa9a4d75116664d319c492c57b4505ec2e147c236040e6433aab4a4/"
                "5bb08ed84b5928be5231419591f99820f157e2e5064e035f189d5b446a71875f.raw"
            ),
            2022: Path(
                "data/v5_2_qd/historical_superseding_565534/raw/quarantine/"
                "mlb_schedule_game_feed/09c51b6670fe7e8c2d0822d752fd46f6068597ad29ff79513615e8d13fa2b03e/"
                "d577ae55106e8ddfca3e5832c0554c896d10d63397c50a396b8046fa1e9a6edf.raw"
            ),
            2023: Path(
                "data/v5_2_qd/historical_superseding_565534/raw/quarantine/"
                "mlb_schedule_game_feed/0ffb733ea25caa4a5ca9dde6c2c9abe11451168fef7afe8e03df4e124c8880f2/"
                "bbc0cd022364dd9cc8d296946a581434a620e30b4c435905981b45f42790dcec.raw"
            ),
            2024: Path(
                "data/v5_2_qd/historical_superseding_565534/raw/quarantine/"
                "mlb_schedule_game_feed/040137af7d63a7187a28d254f9356f48bbeaf897b4f0eabc1ce18ebb6fdd8f3d/"
                "f68a9b238d742d498f016ae3659d2c7180ccfbcb46b0050dd6ffe9dac56946d9.raw"
            ),
        }
        expected = {
            2018: (2485, 2430, 53, 108, 55),
            2022: (2479, 2430, 49, 98, 49),
            2023: (2475, 2430, 45, 90, 45),
            2024: (2469, 2430, 37, 76, 39),
        }
        for year, raw_path in raw_paths.items():
            with self.subTest(year=year):
                raw = (RECOVERY_PARENT_EVIDENCE_ROOT / raw_path).read_bytes()
                occurrences = parse_schedule_occurrences(raw)
                multiplicities = Counter(game["gamePk"] for game in occurrences)
                self.assertEqual(
                    (
                        len(occurrences),
                        len(parse_schedule(raw)),
                        sum(count > 1 for count in multiplicities.values()),
                        sum(count for count in multiplicities.values() if count > 1),
                        len(occurrences) - len(multiplicities),
                    ),
                    expected[year],
                )

    def test_v522_schedule_recompute_requires_exact_authenticated_context(self) -> None:
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        query = dataclasses.replace(_query(), parser_version="v5.2.2")
        with self.assertRaisesRegex(ValueError, "recovery.*context|authority"):
            recompute_schedule_anchor_sidecar(_schedule_raw(), query)
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec, contract = recovery_authority_fixture(
                Path(directory), parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT
            )
            context = _load_static_contracts(run_spec, contract)
            exact_query = dataclasses.replace(
                query,
                run_spec_sha256=context.authority_sha256,
                acquisition_contract_sha256=context.acquisition_contract_sha256,
            )
            sidecar = recompute_schedule_anchor_sidecar(
                _schedule_raw(), exact_query, context=context
            )
            self.assertEqual(sidecar["semantics"]["unique_game_count"], 2)
            duplicate_payload = _schedule_payload()
            duplicate_payload["dates"][0]["games"].append(
                copy.deepcopy(duplicate_payload["dates"][0]["games"][0])
            )
            duplicate_payload["dates"][0]["totalGames"] = 3
            duplicate_payload["totalGames"] = 3
            duplicate = recompute_schedule_anchor_sidecar(
                json.dumps(duplicate_payload, separators=(",", ":")).encode(),
                exact_query,
                context=context,
            )
            self.assertEqual(duplicate["semantics"]["unique_game_count"], 2)
            forged = dataclasses.replace(context, authority_sha256="f" * 64)
            with self.assertRaisesRegex(ValueError, "recovery.*authority"):
                recompute_schedule_anchor_sidecar(
                    _schedule_raw(), exact_query, context=forged
                )

    def test_v522_disposition_schedule_allows_empty_parameters_only_when_authenticated(
        self,
    ) -> None:
        from mlb_backtest.v52_query_plan import (
            _load_static_contracts,
            build_static_batch,
        )
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
        )
        from mlb_backtest.v52_source_contracts import canonical_json_bytes
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec, contract = recovery_authority_fixture(
                Path(directory), parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT
            )
            context = _load_static_contracts(run_spec, contract)
            query = next(
                item
                for item in build_static_batch(run_spec, contract).queries
                if item.endpoint.startswith("disposition://")
            )
            catalog = build_recovery_schedule_conflict_catalog(root, run_spec, contract)
            authority = next(entry for entry in catalog.entries if entry.query == query)
            semantics = authority.semantics.expected_sidecar_semantics
            parent = semantics["old_authority"]
            raw_path = parent["raw"]["path"]
            raw_payload = json.loads((root / raw_path).read_bytes())
            suppressed = semantics.get(
                "suppressed_occurrences",
                [semantics["suppressed_occurrence"]],
            )
            for occurrence in sorted(
                suppressed,
                key=lambda item: (item["date_index"], item["game_index"]),
                reverse=True,
            ):
                date_entry = raw_payload["dates"][occurrence["date_index"]]
                del date_entry["games"][occurrence["game_index"]]
                date_entry["totalGames"] -= 1
                raw_payload["totalGames"] -= 1
            raw = canonical_json_bytes(raw_payload)

            sidecar = recompute_schedule_anchor_sidecar(raw, query, context=context)
            self.assertGreater(sidecar["semantics"]["unique_game_count"], 0)
            with self.assertRaisesRegex(ValueError, "context|authority"):
                recompute_schedule_anchor_sidecar(raw, query)
            with self.assertRaisesRegex(ValueError, "parameters"):
                recompute_schedule_anchor_sidecar(
                    raw,
                    dataclasses.replace(
                        query,
                        endpoint="https://statsapi.mlb.com/api/v1/schedule",
                    ),
                    context=context,
                )
            with self.assertRaisesRegex(ValueError, "authority"):
                recompute_schedule_anchor_sidecar(
                    raw,
                    dataclasses.replace(
                        query,
                        endpoint="disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
                    ),
                    context=context,
                )
            with self.assertRaisesRegex(ValueError, "parameters"):
                recompute_schedule_anchor_sidecar(
                    raw,
                    dataclasses.replace(_query(), parameters={}),
                )

    def test_v522_http_terminal_conflicts_remain_rejected(self) -> None:
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_seasons import SEASON_BOUNDARIES
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        cases = {
            2020: PARENT_RELATIVES["superseding_2020_630882"]["raw"],
            2021: PARENT_RELATIVES["superseding_2021_633224"]["raw"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec, contract = recovery_authority_fixture(
                Path(directory), parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT
            )
            context = _load_static_contracts(run_spec, contract)
            for year, raw_relative in cases.items():
                with self.subTest(year=year):
                    first, last = SEASON_BOUNDARIES[year]
                    query = dataclasses.replace(
                        _query(),
                        parameters={
                            "sportId": 1,
                            "gameType": "R",
                            "startDate": first.isoformat(),
                            "endDate": last.isoformat(),
                            "hydrate": "probablePitcher,lineups,venue,linescore",
                        },
                        requested_range={
                            "year": year,
                            "start_date": first.isoformat(),
                            "end_date": last.isoformat(),
                        },
                        parser_version="v5.2.2",
                        run_spec_sha256=context.authority_sha256,
                        acquisition_contract_sha256=context.acquisition_contract_sha256,
                    )
                    with self.assertRaisesRegex(ValueError, "conflicting duplicate gamePk"):
                        recompute_schedule_anchor_sidecar(
                            (RECOVERY_PARENT_EVIDENCE_ROOT / raw_relative).read_bytes(),
                            query,
                            context=context,
                        )

    def test_schedule_disposition_dispatch_requires_matching_authority_and_empty_raw(self) -> None:
        authority = build_duplicate_disposition_authority(
            SOURCE_AUTHORITY_ROOT,
            source_run_spec_path=SOURCE_AUTHORITY_ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=SOURCE_AUTHORITY_ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        semantics = recompute_query_sidecar(
            authority.query, b"", authority=authority
        )
        self.assertEqual(
            semantics["disposition"], "SCHEDULE_DUPLICATE_GAMEPK_565534"
        )
        with self.assertRaises((TypeError, ValueError)):
            recompute_query_sidecar(authority.query, b"")
        with self.assertRaises((TypeError, ValueError)):
            recompute_query_sidecar(authority.query, b"not-rawless", authority=authority)
        with self.assertRaises((TypeError, ValueError)):
            recompute_query_sidecar(
                authority.query,
                b"",
                authority=dataclasses.replace(
                    authority, evidence_root=ROOT / "data/v5_2_qd/historical/raw"
                ),
            )

    def test_duplicate_disposition_is_rawless_and_has_no_attempt_or_reservation(self) -> None:
        authority = build_duplicate_disposition_authority(
            SOURCE_AUTHORITY_ROOT,
            source_run_spec_path=SOURCE_AUTHORITY_ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=SOURCE_AUTHORITY_ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        protected_old_files = (
            SOURCE_AUTHORITY_ROOT / disposition_module.OLD_REGISTRATION_RELATIVE,
            SOURCE_AUTHORITY_ROOT / disposition_module.OLD_QUARANTINE_RELATIVE,
            SOURCE_AUTHORITY_ROOT / disposition_module.OLD_RAW_RELATIVE,
        )
        old_snapshot = {
            path: (path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in protected_old_files
        }
        self.assertEqual(
            old_snapshot[SOURCE_AUTHORITY_ROOT / disposition_module.OLD_REGISTRATION_RELATIVE][1],
            disposition_module.OLD_REGISTRATION_SHA256,
        )
        self.assertEqual(
            old_snapshot[SOURCE_AUTHORITY_ROOT / disposition_module.OLD_QUARANTINE_RELATIVE][1],
            disposition_module.OLD_QUARANTINE_SHA256,
        )
        self.assertEqual(
            old_snapshot[SOURCE_AUTHORITY_ROOT / disposition_module.OLD_RAW_RELATIVE][1],
            disposition_module.OLD_RAW_SHA256,
        )
        self.assertEqual(authority._read_old_payload()["totalGames"], 2472)
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(Path(td).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            with patch.object(
                disposition_module.DuplicateGamePkDispositionAuthority,
                "reauthenticate",
                return_value=isolated,
            ):
                published = publish_duplicate_gamepk_disposition(store, isolated)
                verified = store.read_verified_success(
                    isolated.query, authority=isolated
                )
            folder = store.root / "queries" / isolated.query.source / published.query_id
            self.assertEqual(published.storage_mode, "DERIVED_DISPOSITION")
            self.assertIsNone(verified.raw_bytes)
            self.assertEqual(verified.attempt_receipts, ())
            self.assertEqual(list((folder / "attempts").iterdir()), [])
            self.assertEqual(list((folder / "objects").iterdir()), [])
            self.assertEqual(verified.success_receipt["attempt_number"], None)
        for path, (contents, digest) in old_snapshot.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_schedule_disposition_schema_is_closed_and_cross_bound_publish_fails(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(Path(td).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            semantics = recompute_query_sidecar(
                authority.query, b"", authority=authority
            )
            with store.acquire_query(isolated.query):
                with self.assertRaisesRegex(TypeError, "authenticated authority"):
                    store.publish_derived_disposition(isolated.query, semantics)
            with store.acquire_query(isolated.query):
                with self.assertRaisesRegex(ValueError, "authority binding"):
                    store.publish_derived_disposition(
                        isolated.query,
                        semantics,
                        authority=dataclasses.replace(
                            isolated,
                            evidence_root=store.root.parent / "other-root",
                        ),
                    )
            folder = store.root / "queries" / isolated.query.source / logical_query_id(isolated.query)
            self.assertEqual(list((folder / "sidecars").iterdir()), [])

            with patch.object(
                disposition_module.DuplicateGamePkDispositionAuthority,
                "reauthenticate",
                return_value=isolated,
            ):
                with store.acquire_query(isolated.query):
                    published = store.publish_derived_disposition(
                        isolated.query, semantics, authority=isolated
                    )
                self.assertEqual(
                    published.query_sidecar_sha256,
                    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1",
                )
                sidecar = json.loads(
                    (store.root / json.loads(published.success_receipt_path.read_bytes())["query_sidecar_path"]).read_bytes()
                )
            schema = json.loads(
                (ROOT / "schemas/v5_2_derived_disposition_sidecar.schema.json").read_bytes()
            )
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(sidecar, schema)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    {**sidecar, "unexpected": True}, schema
                )
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    {**sidecar, "query_id": "a" * 64}, schema
                )
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    {
                        **sidecar,
                        "semantics": {
                            key: value
                            for key, value in sidecar["semantics"].items()
                            if key != "occurrence_counts"
                        },
                    },
                    schema,
                )

    def test_schedule_disposition_final_rebind_rejects_changed_authority_before_success(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(Path(td).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            with patch.object(
                disposition_module.DuplicateGamePkDispositionAuthority,
                "reauthenticate",
                side_effect=[
                    isolated,
                    isolated,
                    isolated,
                    isolated,
                    ValueError("authority changed"),
                ],
            ), self.assertRaisesRegex(ValueError, "authority changed"):
                publish_duplicate_gamepk_disposition(store, isolated)
            folder = store.root / "queries" / isolated.query.source / logical_query_id(isolated.query)
            self.assertFalse((folder / "success.json").exists())
            self.assertEqual(list((folder / "attempts").iterdir()), [])
            self.assertEqual(list((folder / "objects").iterdir()), [])
            self.assertEqual(list((folder / "sidecars").iterdir()), [])

    def test_schedule_sidecar_cleanup_never_deletes_competing_exact_writer(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(Path(td).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            original_write_once = evidence_module._write_once_at
            competing_sidecar: tuple[str, bytes] | None = None
            competing_create_result: bool | None = None
            raced_writer_result: bool | None = None

            def competing_writer(
                parent_fd: int, name: str, contents: bytes, label: str
            ) -> bool:
                nonlocal competing_create_result, competing_sidecar, raced_writer_result
                if label == "derived disposition sidecar":
                    self.assertIsNone(competing_sidecar)
                    competing_create_result = original_write_once(
                        parent_fd, name, contents, label
                    )
                    self.assertTrue(competing_create_result)
                    competing_sidecar = (name, contents)
                    raced_writer_result = original_write_once(
                        parent_fd, name, contents, label
                    )
                    return raced_writer_result
                return original_write_once(parent_fd, name, contents, label)

            with (
                patch.object(
                    disposition_module.DuplicateGamePkDispositionAuthority,
                    "reauthenticate",
                    side_effect=[
                        isolated,
                        isolated,
                        isolated,
                        isolated,
                        ValueError("authority changed"),
                    ],
                ),
                patch.object(
                    evidence_module,
                    "_write_once_at",
                    side_effect=competing_writer,
                ),
                self.assertRaisesRegex(ValueError, "authority changed"),
            ):
                publish_duplicate_gamepk_disposition(store, isolated)
            self.assertIsNotNone(competing_sidecar)
            self.assertTrue(competing_create_result)
            self.assertFalse(raced_writer_result)
            folder = (
                store.root
                / "queries"
                / isolated.query.source
                / logical_query_id(isolated.query)
            )
            self.assertFalse((folder / "success.json").exists())
            name, contents = competing_sidecar
            self.assertEqual((folder / "sidecars" / name).read_bytes(), contents)

    def test_schedule_authority_admission_precedes_recovery_and_cleanup(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        with tempfile.TemporaryDirectory() as td:
            store = V52EvidenceStore(Path(td).resolve() / "raw")
            isolated = dataclasses.replace(authority, evidence_root=store.root)
            semantics = recompute_query_sidecar(
                authority.query, b"", authority=authority
            )
            cross_bound = dataclasses.replace(
                isolated, evidence_root=store.root.parent / "other-root"
            )

            for supplied in (None, cross_bound):
                with self.subTest(operation="publish", authority=supplied):
                    with patch.object(
                        store,
                        "_recover_pending_before_mutation",
                        wraps=store._recover_pending_before_mutation,
                    ) as recovery:
                        with store.acquire_query(isolated.query):
                            with self.assertRaises((TypeError, ValueError)):
                                store.publish_derived_disposition(
                                    isolated.query,
                                    semantics,
                                    authority=supplied,
                                )
                    recovery.assert_not_called()

            with patch.object(
                disposition_module.DuplicateGamePkDispositionAuthority,
                "reauthenticate",
                return_value=isolated,
            ):
                published = publish_duplicate_gamepk_disposition(store, isolated)
            self.assertTrue(published.success_receipt_path.exists())

            for supplied in (None, cross_bound):
                with self.subTest(operation="read", authority=supplied):
                    with patch.object(
                        store,
                        "_finish_failure_cleanup_before_read",
                        wraps=store._finish_failure_cleanup_before_read,
                    ) as cleanup:
                        with self.assertRaises((TypeError, ValueError)):
                            store.read_verified_success(
                                isolated.query, authority=supplied
                            )
                    cleanup.assert_not_called()

                with self.subTest(operation="progress", authority=supplied):
                    with patch.object(
                        store,
                        "_finish_failure_cleanup_tombstones",
                        wraps=store._finish_failure_cleanup_tombstones,
                    ) as cleanup:
                        with self.assertRaises((TypeError, ValueError)):
                            store.read_verified_query_progress(
                                isolated.query, authority=supplied
                            )
                    cleanup.assert_not_called()

    def test_565534_exact_pair_retains_only_resumed_record_and_binds_disposition(self) -> None:
        from mlb_backtest.source_parsers.schedule import parse_schedule

        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        pair = json.loads((DISPOSITION_FIXTURES / "exact_pair.json").read_text())
        self.assertEqual(authority.query.endpoint, DISPOSITION_ENDPOINT)
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            parse_schedule((SOURCE_AUTHORITY_ROOT / authority.old_raw_relative).read_bytes())
        self.assertEqual(
            authority.query.requested_range["suppressed_occurrence"]["sha256"],
            pair["first"]["sha256"],
        )
        self.assertEqual(
            authority.query.requested_range["retained_occurrence"]["sha256"],
            pair["retained"]["sha256"],
        )
        projection = project_schedule_occurrences(authority)
        self.assertEqual(projection.suppressed_position, (54, 5))
        self.assertEqual(projection.retained_position, (161, 12))
        retained = next(game for game in projection.normalized_games if game["game_pk"] == 565534)
        self.assertEqual(retained["scheduled_start_utc"], pair["retained"]["game_date"])
        self.assertEqual(retained["venue_id"], pair["retained"]["venue_id"])

    def test_565534_requires_both_complete_fingerprints_and_all_fixed_fields(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        raw_path = SOURCE_AUTHORITY_ROOT / authority.old_raw_relative
        payload = json.loads(raw_path.read_bytes())
        negative = json.loads(
            (DISPOSITION_FIXTURES / "negative_cases.json").read_text()
        )
        for retained, fixture_key in (
            (False, "changed_suppressed_predicates"),
            (True, "changed_retained_predicates"),
        ):
            for field in negative[fixture_key]:
                changed = copy.deepcopy(payload)
                _mutate_565534_predicate(
                    changed, retained=retained, field=field
                )
                with self.subTest(
                    retained=retained, field=field
                ), self.assertRaisesRegex(ValueError, "565534"):
                    authority.project_payload(changed)

    def test_565534_authority_query_is_json_compatible_and_bound_to_new_contract(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        self.assertEqual(
            logical_query_id(authority.query),
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
        )
        self.assertNotEqual(
            logical_query_id(authority.query),
            "a132f270531aa38ca38c7b053fcce2474c3692eca0fbcee2989a734646900fe7",
        )
        self.assertEqual(
            authority.query.acquisition_contract_sha256,
            "3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58",
        )

    def test_2019_projection_passes_one_ephemeral_canonical_view_to_public_parser(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        raw = json.loads((SOURCE_AUTHORITY_ROOT / authority.old_raw_relative).read_bytes())
        expected_view = _authenticated_filtered_view(raw)
        expected_bytes = disposition_module.canonical_json_bytes(expected_view)
        parser_calls: list[tuple[bytes, list[dict]]] = []
        actual_parse_schedule = disposition_module.parse_schedule

        def observe(value: bytes) -> list[dict]:
            self.assertIsInstance(value, bytes)
            parsed = actual_parse_schedule(value)
            parser_calls.append((value, parsed))
            return parsed

        with patch.object(
            disposition_module, "parse_schedule", side_effect=observe
        ):
            projection = project_schedule_occurrences(authority)
        self.assertEqual(len(parser_calls), 1)
        observed_bytes, parsed = parser_calls[0]
        self.assertEqual(observed_bytes, expected_bytes)
        self.assertEqual(json.loads(observed_bytes), expected_view)
        self.assertEqual(_plain_json(projection.normalized_games), parsed)
        _assert_contains_no_bytes(self, authority)
        _assert_contains_no_bytes(self, projection)

    def test_2019_projection_rejects_reordered_public_parser_before_publication(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        forbidden_namespaces = _superseding_output_namespace_paths()
        before_parent_trees = parent_tree_fingerprints(RECOVERY_PARENT_EVIDENCE_ROOT)
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))
        actual_parse_schedule = disposition_module.parse_schedule

        def reorder(value: bytes) -> list[dict]:
            return list(reversed(actual_parse_schedule(value)))

        with patch.object(disposition_module, "parse_schedule", side_effect=reorder), self.assertRaisesRegex(
            ValueError, "unchanged parse_schedule"
        ):
            project_schedule_occurrences(authority)
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))
        self.assertEqual(
            parent_tree_fingerprints(RECOVERY_PARENT_EVIDENCE_ROOT), before_parent_trees
        )

    def test_2019_publication_wrapper_blocks_reordered_parser_before_store_call(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        store = _RecordingDispositionStore(authority.evidence_root)
        forbidden_namespaces = _superseding_output_namespace_paths()
        before_parent_trees = parent_tree_fingerprints(RECOVERY_PARENT_EVIDENCE_ROOT)
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))
        actual_parse_schedule = disposition_module.parse_schedule

        def reorder(value: bytes) -> list[dict]:
            return list(reversed(actual_parse_schedule(value)))

        with patch.object(disposition_module, "parse_schedule", side_effect=reorder), self.assertRaisesRegex(
            ValueError, "unchanged parse_schedule"
        ):
            publish_duplicate_gamepk_disposition(store, authority)
        self.assertEqual(store.publish_calls, [])
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))
        self.assertEqual(
            parent_tree_fingerprints(RECOVERY_PARENT_EVIDENCE_ROOT), before_parent_trees
        )

    def test_2019_publication_rejects_changed_raw_at_final_authenticated_read(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        raw = json.loads((SOURCE_AUTHORITY_ROOT / authority.old_raw_relative).read_bytes())
        ignored_copy = raw["dates"][4]["games"][14]
        self.assertEqual(ignored_copy["gamePk"], 565221)
        ignored_copy["calendarEventID"] = "tampered-ignored-copy"
        tampered_raw = json.dumps(
            raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        store = _RecordingDispositionStore(authority.evidence_root)
        original_read = disposition_module._read_regular_single_link
        raw_read_count = 0

        def alternate_raw_read(
            root: Path, relative: Path, label: str
        ) -> bytes:
            nonlocal raw_read_count
            if relative == disposition_module.OLD_RAW_RELATIVE:
                raw_read_count += 1
                if raw_read_count == 2:
                    return tampered_raw
            return original_read(root, relative, label)

        with patch.object(
            disposition_module,
            "_read_regular_single_link",
            side_effect=alternate_raw_read,
        ), self.assertRaisesRegex(ValueError, "sha256 differs"):
            publish_duplicate_gamepk_disposition(store, authority)
        self.assertEqual(raw_read_count, 2)
        self.assertEqual(store.publish_calls, [])

    def test_2019_projection_deep_freezes_nested_normalized_games(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        projection = project_schedule_occurrences(authority)
        game = next(
            row
            for row in projection.normalized_games
            if row["away_lineup"] and row["home_lineup"]
        )
        away_player = game["away_lineup"][0]
        away_key = next(iter(away_player))
        with self.assertRaises(TypeError):
            away_player[away_key] = "mutated"
        with self.assertRaises(AttributeError):
            game["home_lineup"].append({})

    def test_nonordinary_duplicate_gamepk_or_third_special_copy_fails_closed(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        payload = json.loads((SOURCE_AUTHORITY_ROOT / authority.old_raw_relative).read_bytes())
        third = copy.deepcopy(payload)
        third["dates"][161]["games"].append(copy.deepcopy(third["dates"][161]["games"][12]))
        third["dates"][161]["totalGames"] += 1
        third["totalGames"] += 1
        with self.assertRaisesRegex(ValueError, "565534"):
            authority.project_payload(third)
        irreconcilable = copy.deepcopy(payload)
        duplicate = copy.deepcopy(irreconcilable["dates"][0]["games"][0])
        duplicate["teams"]["home"]["team"]["id"] += 10000
        irreconcilable["dates"][1]["games"].append(duplicate)
        irreconcilable["dates"][1]["totalGames"] += 1
        irreconcilable["totalGames"] += 1
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            authority.project_payload(irreconcilable, expected_counts=None)
        negative = json.loads(
            (DISPOSITION_FIXTURES / "negative_cases.json").read_text()
        )
        for total in negative["rejected_projection_totals"]:
            wrong_total = copy.deepcopy(payload)
            wrong_total["totalGames"] = total
            with self.subTest(total=total), self.assertRaisesRegex(
                ValueError, "occurrence totals"
            ):
                authority.project_payload(wrong_total)

    def test_2019_projection_authenticates_2472_occurrences_42_duplicates_and_2430_unique(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        projection = project_schedule_occurrences(authority)
        self.assertEqual(dict(projection.occurrence_counts), EXPECTED_OCCURRENCE_COUNTS)
        self.assertEqual(len(projection.normalized_games), 2430)
        self.assertEqual(len(projection.selected_raw_game_object_sha256s), 2430)
        ordinary = json.loads(
            (DISPOSITION_FIXTURES / "ordinary_games.json").read_text()
        )
        self.assertEqual(
            projection.occurrence_counts[
                "ordinary_reconciled_duplicate_game_pks"
            ],
            ordinary["ordinary_pair_count_after_special_suppression"],
        )
        raw = json.loads((SOURCE_AUTHORITY_ROOT / authority.old_raw_relative).read_bytes())
        for representative in ordinary["representatives"]:
            positions = representative["positions"]
            self.assertTrue(
                all(
                    raw["dates"][date_index]["games"][game_index]["gamePk"]
                    == representative["game_pk"]
                    for date_index, game_index in positions
                )
            )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    _plain_json(projection.normalized_games),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            NORMALIZED_GAMES_SHA256,
        )
        selected = sorted(
            (dict(row) for row in projection.selected_raw_game_object_sha256s),
            key=lambda row: int(row["game_pk"]),
        )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    list(selected), separators=(",", ":"), sort_keys=True
                ).encode()
            ).hexdigest(),
            SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256,
        )

    def test_r3_combined_2021_projection_excludes_missing_venue_before_parsing(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )
        from mlb_backtest.v52_sources.schedule import (
            project_r3_missing_venue_schedule,
        )

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        parent = build_recovery_parent_catalog(RECOVERY_PARENT_EVIDENCE_ROOT)
        projection = project_r3_missing_venue_schedule(
            parent.read_raw("superseding_2021_633224"),
            catalog.projection_query_for_game_pk(633224),
            catalog,
        )

        self.assertEqual(projection.pre_normalized_games_count, 2430)
        self.assertEqual(
            projection.pre_normalized_games_sha256,
            "891f73f68c3108ccf78f11f0ee11125bd6fdb4dc71e17bddadbcb8d88c008769",
        )
        self.assertEqual(
            projection.pre_selected_raw_game_object_sha256s_sha256,
            "17362576e0ae9e96a0e20979a9f8afdfbb95de03e2aa3fd4d66a4aa63486681f",
        )
        self.assertEqual(projection.post_normalized_games_count, 2429)
        self.assertEqual(
            projection.post_normalized_games_sha256,
            "8fc70974fcf58cacbd017c8a00e52cedc0f53c99bd6b035850d8e694e2f17263",
        )
        self.assertEqual(
            projection.post_selected_raw_game_object_sha256s_sha256,
            "b66766a1395986a3a49e39708fe0de00702d93c1d13985a6018ac8f86d5fd148",
        )
        self.assertEqual(projection.excluded_game_pks, frozenset({632924}))
        self.assertNotIn(
            632924,
            {
                game["game_pk"]
                for game in parse_schedule(projection.raw)
            },
        )

    def test_r3_projected_2021_bytes_require_the_combined_authenticated_query(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )
        from mlb_backtest.v52_sources.schedule import (
            project_r3_missing_venue_schedule,
            recompute_r3_missing_venue_schedule_anchor_sidecar,
            recompute_schedule_anchor_sidecar,
        )

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        query = catalog.projection_query_for_game_pk(632924)
        raw = build_recovery_parent_catalog(RECOVERY_PARENT_EVIDENCE_ROOT).read_raw(
            "superseding_2021_633224"
        )
        projection = project_r3_missing_venue_schedule(raw, query, catalog)
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )

        with self.assertRaisesRegex(ValueError, "sealed catalog projection"):
            recompute_schedule_anchor_sidecar(projection.raw, query, context=context)
        sidecar = recompute_r3_missing_venue_schedule_anchor_sidecar(
            raw, query, catalog, context=context
        )
        self.assertNotIn(632924, sidecar["semantics"]["game_pks"])
        self.assertEqual(sidecar["semantics"]["unique_game_count"], 2429)
        self.assertEqual(
            sidecar["semantics"]["recovery_disposition"]["excluded_game_pks"],
            [632924],
        )

    def test_r3_combined_query_sidecar_is_rawless_and_excludes_missing_venue(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        query = catalog.projection_query_for_game_pk(632924)
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )

        semantics = recompute_query_sidecar(
            query, b"", authority=catalog, context=context
        )
        self.assertNotIn(632924, semantics["game_pks"])
        self.assertEqual(semantics["unique_game_count"], 2429)
        self.assertEqual(
            semantics["recovery_disposition"]["excluded_game_pks"], [632924]
        )
        r2_context = _load_static_contracts(
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_recovery_schedule_conflicts_r2.json",
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_recovery_schedule_conflicts_r2.json",
        )
        with self.assertRaises(ValueError):
            recompute_query_sidecar(
                query, b"", authority=catalog, context=r2_context
            )

    def test_r3_catalog_dispatches_every_projection_query_and_no_standalone_exclusion(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        projection = catalog.as_projection()
        self.assertEqual(len(projection.projection_query_ids), 3)
        for query in (
            catalog.projection_query_for_game_pk(565534),
            catalog.projection_query_for_game_pk(630882),
            catalog.projection_query_for_game_pk(632924),
        ):
            with self.subTest(query=query.endpoint):
                semantics = recompute_query_sidecar(
                    query, b"", authority=catalog, context=context
                )
                self.assertNotIn(632924, semantics["game_pks"])
        standalone = dataclasses.replace(
            catalog.projection_query_for_game_pk(632924),
            endpoint="disposition://MISSING_VENUE_632924",
        )
        with self.assertRaises((TypeError, ValueError)):
            recompute_query_sidecar(
                standalone, b"", authority=catalog, context=context
            )

    def test_r3_combined_recompute_projects_once_per_public_call(self) -> None:
        import mlb_backtest.v52_sources.schedule as schedule_module
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE, R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts

        catalog = build_missing_venue_recovery_disposition_catalog(ROOT, ROOT / R3_RUN_SPEC_RELATIVE, ROOT / R3_ACQUISITION_CONTRACT_RELATIVE)
        context = _load_static_contracts(ROOT / R3_RUN_SPEC_RELATIVE, ROOT / R3_ACQUISITION_CONTRACT_RELATIVE)
        query = catalog.projection_query_for_game_pk(632924)
        original = schedule_module.project_r3_missing_venue_schedule
        with patch.object(schedule_module, "project_r3_missing_venue_schedule", wraps=original) as projected:
            recompute_query_sidecar(query, b"", authority=catalog, context=context)
        self.assertEqual(projected.call_count, 1)

    def test_r3_catalog_publishes_and_reverifies_all_projection_sidecars(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        queries = tuple(
            catalog.projection_query_for_game_pk(game_pk)
            for game_pk in (565534, 630882, 632924)
        )
        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            for query in queries:
                semantics = recompute_query_sidecar(
                    query, b"", authority=catalog, context=context
                )
                with store.acquire_query(query):
                    published = store.publish_derived_disposition(
                        query,
                        semantics,
                        authority=catalog,
                        context=context,
                    )
                verified = store.read_verified_success(
                    query, authority=catalog, context=context
                )
                self.assertEqual(verified.published_evidence, published)
                self.assertIsNone(verified.raw_bytes)
                self.assertEqual(verified.query_sidecar["semantics"], semantics)
            query_directories = tuple(
                (store.root / "queries" / queries[0].source).iterdir()
            )
            self.assertEqual(len(query_directories), 3)

    def test_r3_public_sidecar_rejects_fabricated_projection(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )
        from mlb_backtest.v52_sources.schedule import (
            project_r3_missing_venue_schedule,
            recompute_schedule_anchor_sidecar,
        )

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        query = catalog.projection_query_for_game_pk(632924)
        raw = build_recovery_parent_catalog(RECOVERY_PARENT_EVIDENCE_ROOT).read_raw(
            "superseding_2021_633224"
        )
        projection = project_r3_missing_venue_schedule(raw, query, catalog)
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        for label, forged in (
            ("excluded", dataclasses.replace(projection, excluded_game_pks=frozenset())),
            ("count", dataclasses.replace(projection, post_normalized_games_count=2430)),
            ("digest", dataclasses.replace(projection, post_normalized_games_sha256="0" * 64)),
        ):
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    recompute_schedule_anchor_sidecar(
                        forged.raw, query, context=context, r3_projection=forged
                    )

    def test_r3_private_sidecar_rejects_each_forged_projection_field(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_query_plan import _load_static_contracts
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )
        from mlb_backtest.v52_sources.schedule import (
            _recompute_schedule_anchor_sidecar,
            project_r3_missing_venue_schedule,
        )

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        query = catalog.projection_query_for_game_pk(632924)
        projection = project_r3_missing_venue_schedule(
            build_recovery_parent_catalog(RECOVERY_PARENT_EVIDENCE_ROOT).read_raw(
                "superseding_2021_633224"
            ),
            query,
            catalog,
        )
        context = _load_static_contracts(
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        for label, forged in (
            ("raw", dataclasses.replace(projection, raw=b"{}")),
            ("excluded", dataclasses.replace(projection, excluded_game_pks=frozenset())),
            ("pre-count", dataclasses.replace(projection, pre_normalized_games_count=0)),
            ("post-count", dataclasses.replace(projection, post_normalized_games_count=0)),
            ("pre-digest", dataclasses.replace(projection, pre_normalized_games_sha256="0" * 64)),
            ("post-digest", dataclasses.replace(projection, post_normalized_games_sha256="0" * 64)),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    _recompute_schedule_anchor_sidecar(
                        forged.raw,
                        query,
                        context=context,
                        r3_projection=forged,
                    )

    def test_r3_projection_does_not_expose_mutable_normalized_games(self) -> None:
        from mlb_backtest.v52_missing_venue_recovery_disposition import (
            R3_ACQUISITION_CONTRACT_RELATIVE,
            R3_RUN_SPEC_RELATIVE,
            build_missing_venue_recovery_disposition_catalog,
        )
        from mlb_backtest.v52_recovery_parent_evidence import (
            build_recovery_parent_catalog,
        )
        from mlb_backtest.v52_sources.schedule import project_r3_missing_venue_schedule

        catalog = build_missing_venue_recovery_disposition_catalog(
            ROOT,
            ROOT / R3_RUN_SPEC_RELATIVE,
            ROOT / R3_ACQUISITION_CONTRACT_RELATIVE,
        )
        projection = project_r3_missing_venue_schedule(
            build_recovery_parent_catalog(RECOVERY_PARENT_EVIDENCE_ROOT).read_raw(
                "superseding_2021_633224"
            ),
            catalog.projection_query_for_game_pk(632924),
            catalog,
        )

        self.assertFalse(hasattr(projection, "normalized_games"))
        with self.assertRaises(AttributeError):
            projection.normalized_games.append({})
        with self.assertRaises(AttributeError):
            projection.__dict__

    def test_schedule_anchor_seals_complete_population_without_boxscores(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        raw = _schedule_raw()
        sidecar = recompute_schedule_anchor_sidecar(raw, _query())
        self.assertEqual(sidecar["query_id"], logical_query_id(_query()))
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["game_pks"], [9001, 9002])
        self.assertEqual(semantics["played_game_pks"], [9001])
        self.assertEqual(semantics["unplayed_game_pks"], [9002])
        self.assertEqual(semantics["unique_game_count"], 2)
        self.assertEqual(semantics["anchors"][1]["doubleheader_order"], 2)
        self.assertTrue(semantics["anchors"][1]["neutral_site"])
        self.assertTrue(semantics["anchors"][1]["alternate_site"])
        self.assertEqual(
            semantics["anchors"][0]["winner_team_id"], 20
        )
        requirements = semantics["child_query_requirements"]
        self.assertEqual(
            [row["game_pk"] for row in requirements["boxscores"]], [9001]
        )
        self.assertEqual([row["venue_id"] for row in requirements["venues"]], [31, 99])
        self.assertEqual(sidecar, recompute_schedule_anchor_sidecar(raw, _query()))

    def test_schedule_anchor_rejects_wrong_range_duplicate_and_conflict(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        payload = _schedule_payload()
        duplicate = copy.deepcopy(payload)
        duplicate["dates"][0]["games"].append(copy.deepcopy(duplicate["dates"][0]["games"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            recompute_schedule_anchor_sidecar(json.dumps(duplicate).encode(), _query())
        conflict = copy.deepcopy(payload)
        extra = copy.deepcopy(conflict["dates"][0]["games"][0])
        extra["teams"]["home"]["team"]["id"] = 21
        conflict["dates"][0]["games"].append(extra)
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            recompute_schedule_anchor_sidecar(json.dumps(conflict).encode(), _query())
        with self.assertRaisesRegex(ValueError, "outside its query"):
            wrong_query = _query()
            recompute_schedule_anchor_sidecar(
                _schedule_raw(),
                wrong_query.__class__(**{**wrong_query.__dict__, "requested_range": {"year": 2023, "start_date": "2023-03-30", "end_date": "2023-10-01"}, "parameters": {**wrong_query.parameters, "startDate": "2023-03-30", "endDate": "2023-10-01"}}),
            )
        with self.assertRaisesRegex(ValueError, "no regular-season games"):
            recompute_schedule_anchor_sidecar(b'{"totalGames":0,"dates":[]}', _query())
        with self.assertRaisesRegex(ValueError, "totalGames"):
            recompute_schedule_anchor_sidecar(
                (FIXTURES / "schedule_mini.json").read_bytes(), _query()
            )
        truncated = _schedule_payload()
        truncated["totalGames"] = 3
        with self.assertRaisesRegex(ValueError, "population"):
            recompute_schedule_anchor_sidecar(json.dumps(truncated).encode(), _query())
        for field in ("neutralSite", "isAlternateSite"):
            non_boolean_site = _schedule_payload()
            non_boolean_site["dates"][0]["games"][0][field] = 0
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                recompute_schedule_anchor_sidecar(
                    json.dumps(non_boolean_site).encode(), _query()
                )

    def test_schedule_anchor_defaults_omitted_official_site_flags_to_false(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        payload = _schedule_payload()
        ordinary = payload["dates"][0]["games"][0]
        ordinary.pop("neutralSite")
        ordinary.pop("isAlternateSite")
        sidecar = recompute_schedule_anchor_sidecar(
            json.dumps(payload).encode(), _query()
        )
        ordinary_anchor, special_anchor = sidecar["semantics"]["anchors"]
        self.assertFalse(ordinary_anchor["neutral_site"])
        self.assertFalse(ordinary_anchor["alternate_site"])
        self.assertTrue(special_anchor["neutral_site"])
        self.assertTrue(special_anchor["alternate_site"])

    def test_completed_early_rejects_tied_or_contradictory_winner_evidence(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_schedule_anchor_sidecar,
        )

        contradiction = _schedule_payload()
        game = contradiction["dates"][0]["games"][0]
        game["status"]["detailedState"] = "Completed Early"
        game["teams"]["away"]["score"] = 5
        game["teams"]["home"]["score"] = 4
        with self.assertRaisesRegex(ValueError, "winner"):
            recompute_schedule_anchor_sidecar(json.dumps(contradiction).encode(), _query())

        tied = json.loads(_boxscore_raw())
        tied["teams"]["away"]["teamStats"]["batting"]["runs"] = 4
        with self.assertRaisesRegex(ValueError, "winner"):
            recompute_boxscore_sidecar(json.dumps(tied).encode(), _box_query())

    def test_game_snapshot_joins_verified_anchors_and_final_participants(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            _schedule_raw(), _query()
        )
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar
        boxscore_sidecar = recompute_boxscore_sidecar(_boxscore_raw(), _box_query())
        boxscore = _artifact(dict(boxscore_sidecar))
        snapshot = recompute_game_evidence_snapshot(
            [anchor], {9001: boxscore}
        )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        played, unplayed = snapshot["games"]
        self.assertEqual(played["verification_status"], "VERIFIED")
        self.assertEqual(
            played["participant_provenance"], "RECONSTRUCTED_FINAL_PARTICIPANTS"
        )
        self.assertNotEqual(played["participant_provenance"], "T_MINUS_45")
        self.assertEqual(played["actual_starter_ids"], [101, 201])
        self.assertEqual(played["terminal_status"], "Final")
        self.assertEqual(played["completed_innings"], 9)
        self.assertEqual(len(played["away_starting_hitter_ids"]), 9)
        self.assertEqual(len(played["home_starting_hitter_ids"]), 9)
        self.assertEqual(
            unplayed["participant_provenance"], "HISTORICAL_NO_PARTICIPANTS"
        )
        self.assertTrue(unplayed["neutral_site"])
        self.assertRegex(played["schedule_query_sidecar_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(played["boxscore_query_sidecar_sha256"], boxscore[1])
        self.assertEqual(
            set(snapshot["source_query_sidecar_sha256s"]),
            {
                played["schedule_query_sidecar_sha256"],
                played["boxscore_query_sidecar_sha256"],
            },
        )

    def test_game_snapshot_carries_source_anchor_provenance_only_when_supplied(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(_schedule_raw(), _query())
        boxscore = _artifact(
            dict(recompute_boxscore_sidecar(_boxscore_raw(), _box_query()))
        )
        old = recompute_game_evidence_snapshot([anchor], {9001: boxscore})
        provenance = _test_schedule_provenance(anchor)
        source = recompute_game_evidence_snapshot(
            [anchor], {9001: boxscore}, schedule_provenance=provenance
        )

        old_played, old_unplayed = old["games"]
        source_played, source_unplayed = source["games"]
        self.assertNotIn("raw_game_object_sha256", old_played)
        self.assertNotIn("normalized_anchor_sha256", old_played)
        self.assertNotIn("raw_game_object_sha256", old_unplayed)
        self.assertNotIn("normalized_anchor_sha256", old_unplayed)
        self.assertEqual(
            source_played["raw_game_object_sha256"],
            provenance[9001]["raw_game_object_sha256"],
        )
        self.assertEqual(
            source_played["normalized_anchor_sha256"],
            provenance[9001]["normalized_anchor_sha256"],
        )
        self.assertNotIn("raw_game_object_sha256", source_unplayed)
        self.assertNotIn("normalized_anchor_sha256", source_unplayed)
        self.assertNotEqual(
            source_played["source_game_sha256"], old_played["source_game_sha256"]
        )
        jsonschema.validate(old, json.loads(SCHEMA.read_text()))
        jsonschema.validate(source, json.loads(SCHEMA.read_text()))

    def test_game_snapshot_rejects_incomplete_schedule_provenance(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(_schedule_raw(), _query())
        boxscore = _artifact(
            dict(recompute_boxscore_sidecar(_boxscore_raw(), _box_query()))
        )
        provenance = _test_schedule_provenance(anchor)
        for label, mutate in (
            ("empty", lambda value: value.clear()),
            ("incomplete", lambda value: value.pop(9002)),
            (
                "extra",
                lambda value: value.update(
                    {9003: {
                        "game_pk": 9003,
                        "official_date": "2024-04-04",
                        "away_team_id": 10,
                        "home_team_id": 20,
                        "raw_game_object_sha256": "a" * 64,
                        "normalized_anchor_sha256": "b" * 64,
                    }}
                ),
            ),
            (
                "identity",
                lambda value: value[9001].update({"away_team_id": 999}),
            ),
            (
                "missing-raw-hash-key",
                lambda value: value[9001].pop("raw_game_object_sha256"),
            ),
            (
                "missing-normalized-hash-key",
                lambda value: value[9001].pop("normalized_anchor_sha256"),
            ),
            (
                "extra-record-key",
                lambda value: value[9001].update({"unexpected": True}),
            ),
            (
                "truncated-raw-hash",
                lambda value: value[9001].update(
                    {"raw_game_object_sha256": "a" * 63}
                ),
            ),
            (
                "uppercase-raw-hash",
                lambda value: value[9001].update(
                    {"raw_game_object_sha256": "A" * 64}
                ),
            ),
            (
                "malformed-raw-hash",
                lambda value: value[9001].update(
                    {"raw_game_object_sha256": "g" * 64}
                ),
            ),
            (
                "truncated-normalized-hash",
                lambda value: value[9001].update(
                    {"normalized_anchor_sha256": "b" * 63}
                ),
            ),
            (
                "uppercase-normalized-hash",
                lambda value: value[9001].update(
                    {"normalized_anchor_sha256": "B" * 64}
                ),
            ),
            (
                "malformed-normalized-hash",
                lambda value: value[9001].update(
                    {"normalized_anchor_sha256": "g" * 64}
                ),
            ),
        ):
            candidate = copy.deepcopy(provenance)
            mutate(candidate)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "provenance"):
                recompute_game_evidence_snapshot(
                    [anchor], {9001: boxscore}, schedule_provenance=candidate
                )

    def test_game_snapshot_rejects_missing_boxscore_and_identity_disagreement(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            _schedule_raw(), _query()
        )
        with self.assertRaisesRegex(ValueError, "missing boxscore"):
            recompute_game_evidence_snapshot([anchor], {})
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar
        valid_sidecar = dict(recompute_boxscore_sidecar(_boxscore_raw(), _box_query()))
        valid_bytes, _ = _artifact(valid_sidecar)
        with self.assertRaisesRegex(ValueError, "verified hash"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: (valid_bytes, "0" * 64)},
            )
        no_newline = valid_bytes.removesuffix(b"\n")
        with self.assertRaisesRegex(ValueError, "lineage"):
            recompute_game_evidence_snapshot(
                [anchor],
                {9001: (no_newline, hashlib.sha256(no_newline).hexdigest())},
            )
        wrong_query = copy.deepcopy(valid_sidecar)
        wrong_query["query_id"] = "not-a-query-id"
        with self.assertRaisesRegex(ValueError, "lineage"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: _artifact(wrong_query)}
            )
        wrong_game = copy.deepcopy(valid_sidecar)
        wrong_game["semantics"]["game_pk"] = 9002
        with self.assertRaisesRegex(ValueError, "gamePk"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: _artifact(wrong_game)}
            )
        boxscore = json.loads(_boxscore_raw())
        boxscore["teams"]["home"]["team"]["id"] = 21
        raw = json.dumps(boxscore).encode()
        artifact = _artifact(dict(recompute_boxscore_sidecar(raw, _box_query())))
        with self.assertRaisesRegex(ValueError, "disagrees"):
            recompute_game_evidence_snapshot([anchor], {9001: artifact})
        scores = json.loads(_boxscore_raw())
        scores["teams"]["home"]["teamStats"]["batting"]["runs"] = 3
        score_raw = json.dumps(scores).encode()
        score_artifact = _artifact(dict(recompute_boxscore_sidecar(score_raw, _box_query())))
        with self.assertRaisesRegex(ValueError, "terminal"):
            recompute_game_evidence_snapshot([anchor], {9001: score_artifact})

    def test_query_dispatch_seals_one_boxscore_response_independently(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        query = _box_query()
        semantics = recompute_query_sidecar(
            query, _boxscore_raw()
        )
        self.assertEqual(semantics["game_pk"], 9001)
        self.assertEqual(semantics["actual_starter_ids"], [101, 201])
        self.assertNotIn("terminal_status", semantics)
        self.assertNotIn("completed_innings", semantics)

    def test_official_boxscore_ignores_substitution_orders_and_rejects_bad_slots(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar

        sidecar = recompute_boxscore_sidecar(_boxscore_raw(), _box_query())
        self.assertEqual(
            sidecar["semantics"]["away_starting_hitter_ids"],
            list(range(1001, 1010)),
        )
        payload = json.loads(_boxscore_raw())
        duplicate = copy.deepcopy(payload)
        duplicate["teams"]["away"]["players"]["ID1099"] = {
            "person": {"id": 1099},
            "battingOrder": "100",
        }
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            recompute_boxscore_sidecar(json.dumps(duplicate).encode(), _box_query())
        missing = copy.deepcopy(payload)
        missing["teams"]["away"]["players"]["ID1001"].pop("battingOrder")
        with self.assertRaisesRegex(ValueError, "nine unique"):
            recompute_boxscore_sidecar(json.dumps(missing).encode(), _box_query())

    def test_game_snapshot_rejects_empty_schedule_population(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_game_evidence_snapshot

        with self.assertRaisesRegex(ValueError, "nonempty"):
            recompute_game_evidence_snapshot([], {})


class RecoveryDispositionCatalogTests(unittest.TestCase):
    """The recovery catalog is closed before any derived sidecar is published."""

    @classmethod
    def setUpClass(cls) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
        )

        cls._temporary = tempfile.TemporaryDirectory()
        cls.repository, cls.run_spec, cls.contract = recovery_authority_fixture(
            Path(cls._temporary.name),
            parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT,
        )
        cls.catalog = build_recovery_schedule_conflict_catalog(
            cls.repository, cls.run_spec, cls.contract
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_catalog_is_the_exact_gamepk_ordered_three_entry_set(self) -> None:
        entries = self.catalog.entries
        self.assertEqual(
            tuple(entry.game_pk for entry in entries), (565534, 630882, 633224)
        )
        self.assertEqual(
            tuple(entry.query.endpoint for entry in entries),
            (
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
                "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
            ),
        )
        expected = (
            "1c8fd832cd18285d3ff35b1920ec9e0d1073550132e11235c636f404531711db",
            "5ea730594c017b0cde008b3a9a8826355ea0f00724a7c768693f50259306ea14",
            "6f40def4a04d8373f45dcea125b2100cb684415e3f7b14f5a737646d548cb1c9",
        )
        self.assertEqual(self.catalog.query_ids(), expected)
        self.assertEqual(
            tuple(sorted(self.catalog.query_ids())), tuple(sorted(expected))
        )
        self.assertFalse(
            (
                self.repository
                / "data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw"
            ).exists()
        )

    def test_catalog_queries_are_immediately_deeply_immutable(self) -> None:
        for authority in self.catalog.entries:
            with self.subTest(game_pk=authority.game_pk):
                query = authority.query
                query_id = logical_query_id(query)
                self.assertEqual(query, authority.semantics.query)
                with self.assertRaises(TypeError):
                    query.parameters["forged"] = True
                with self.assertRaises(TypeError):
                    query.requested_range["year"] = 1900
                self.assertEqual(logical_query_id(query), query_id)

    def test_catalog_recomputes_exact_boundary_semantics_without_recovery_output(
        self,
    ) -> None:
        by_game = {entry.game_pk: entry for entry in self.catalog.entries}
        old, twenty, twenty_one = by_game[565534], by_game[630882], by_game[633224]
        self.assertEqual(old.semantics.played_date, None)
        self.assertEqual(old.semantics.result_available_on, None)
        self.assertEqual(
            twenty.semantics.retained_scheduled_start_utc, "2020-08-14T21:05:00Z"
        )
        self.assertEqual(twenty.semantics.played_date, "2020-08-14")
        self.assertEqual(twenty.semantics.result_available_on, "2020-08-14")
        self.assertEqual(
            twenty_one.semantics.retained_scheduled_start_utc, "2021-09-25T00:10:00Z"
        )
        self.assertEqual(twenty_one.semantics.played_date, "2021-09-24")
        self.assertEqual(twenty_one.semantics.result_available_on, "2021-09-24")
        self.assertEqual(
            twenty.semantics.raw_object_sha256s,
            (
                "2a5b251745546e643897cfc01eaa4c63c949bb8d8243b7fec44d3a23484f9be4",
                "0d961f12abb78e3b833c9f5c5bbbff76cad80a5370f57786904c7b42e82b204e",
            ),
        )
        self.assertEqual(
            twenty_one.semantics.raw_object_sha256s,
            (
                "b5d52997a362f89e669e1454960b5230d4977f30370ad3f3ad7ca1b12be5221d",
                "25f8ab0769219c592779cd800bb2b3278c9c75644647bd51ae5db3051bcf5a61",
                "109a5d145731938e08f516f19345557894316b3a72687f4ac63029df07afe94c",
            ),
        )
        self.assertEqual(twenty.semantics.result_available_on < "2020-08-15", True)
        self.assertEqual(twenty_one.semantics.result_available_on < "2021-09-25", True)

    def test_reauthentication_rejects_each_mutated_closed_semantic(self) -> None:
        import dataclasses
        from types import MappingProxyType

        authority = self.catalog.for_game_pk(630882)
        changed_semantics = (
            dataclasses.replace(
                authority.semantics,
                query=dataclasses.replace(
                    authority.query, endpoint="disposition://forged"
                ),
            ),
            dataclasses.replace(authority.semantics, raw_object_sha256s=("0" * 64,)),
            dataclasses.replace(
                authority.semantics,
                expected_sidecar_semantics=MappingProxyType(
                    {"terminal_status": "Scheduled"}
                ),
            ),
            dataclasses.replace(
                authority.semantics,
                expected_derived_sidecar=MappingProxyType(
                    {"parent_query_id": "0" * 64}
                ),
            ),
            dataclasses.replace(authority.semantics, played_date="2020-08-15"),
            dataclasses.replace(authority.semantics, result_available_on="2020-08-15"),
        )
        for semantics in changed_semantics:
            with self.subTest(semantics=semantics):
                with self.assertRaisesRegex(ValueError, "changed|differs|identity"):
                    dataclasses.replace(authority, semantics=semantics).reauthenticate()

    def test_reauthentication_rejects_nested_occurrence_parent_and_count_mutations(
        self,
    ) -> None:
        authority = self.catalog.for_game_pk(630882)
        cases: dict[str, tuple[tuple[str, ...], object]] = {
            "occurrence_index": (
                ("suppressed_occurrence", "date_index"),
                18,
            ),
            "parent_registration_path": (
                ("old_authority", "query_registration", "path"),
                "data/forged/query.json",
            ),
            "parent_raw_hash": (
                ("old_authority", "raw", "sha256"),
                "0" * 64,
            ),
            "occurrence_status": (
                ("fixed_game_facts", "terminal_status"),
                "Scheduled",
            ),
            "occurrence_count": (
                ("occurrence_counts", "projected_occurrences"),
                971,
            ),
        }
        for label, (path, replacement) in cases.items():
            with self.subTest(label=label):
                semantics = _plain_json(authority.semantics.expected_sidecar_semantics)
                target = semantics
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                changed = dataclasses.replace(
                    authority.semantics,
                    expected_sidecar_semantics=semantics,
                )
                with self.assertRaisesRegex(ValueError, "changed|differs|identity"):
                    dataclasses.replace(authority, semantics=changed).reauthenticate()

    def test_lookup_fails_closed_for_unknown_or_duplicate_identity(self) -> None:
        import dataclasses

        with self.assertRaisesRegex(ValueError, "game_pk"):
            self.catalog.for_game_pk(1)
        with self.assertRaisesRegex(ValueError, "query"):
            self.catalog.for_query(
                dataclasses.replace(
                    self.catalog.entries[0].query, endpoint="disposition://forged"
                )
            )
        duplicate_entry = dataclasses.replace(
            self.catalog.entries[1],
            query=self.catalog.entries[0].query,
            semantics=dataclasses.replace(
                self.catalog.entries[1].semantics,
                query=self.catalog.entries[0].query,
            ),
        )
        duplicate = dataclasses.replace(
            self.catalog,
            entries=(self.catalog.entries[0], duplicate_entry, self.catalog.entries[2]),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            duplicate.for_query(self.catalog.entries[0].query)

    def test_derived_sidecar_schema_has_three_closed_recovery_branches(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_derived_disposition_sidecar.schema.json").read_bytes()
        )
        self.assertEqual(len(schema["oneOf"]), 5)
        for authority in self.catalog.entries:
            sidecar = _plain_json(authority.semantics.expected_derived_sidecar)
            jsonschema.validate(sidecar, schema)
            sidecar["semantics"]["unexpected"] = True
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(sidecar, schema)

    def test_stale_source_authority_is_materialized_without_mutating_evidence(self) -> None:
        from tests.v52_recovery_schedule_conflict_fixture import (
            PARENT_RELATIVES,
            matching_source_authority_root,
        )

        original = CONFIGURED_SOURCE_AUTHORITY_ROOT
        protected = _schedule_feed_evidence_files(original)
        self.assertTrue(protected)
        for namespace in (
            Path("data/v5_2_qd/historical"),
            Path("data/v5_2_qd/historical_superseding_565534"),
        ):
            self.assertTrue(
                any(path.is_relative_to(original / namespace) for path in protected)
            )
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "stale-source-authority"
            shutil.copytree(
                ROOT,
                stale,
                ignore=shutil.ignore_patterns(
                    ".pytest_cache", ".ruff_cache", "__pycache__", ".task3-review.*"
                ),
            )
            for relative in {
                path.parent
                for path in PARENT_RELATIVES["original_2019_565534"].values()
            }:
                shutil.copytree(original / relative, stale / relative)
            stale_schema = stale / "schemas/v5_2_acquisition_contract.schema.json"
            schema = json.loads(stale_schema.read_bytes())
            schema["oneOf"] = schema["oneOf"][:-1]
            stale_schema.write_text(
                json.dumps(
                    schema,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            stale_before = hashlib.sha256(stale_schema.read_bytes()).hexdigest()
            materialized = matching_source_authority_root(ROOT, stale)
            self.assertNotEqual(materialized, stale)
            self.assertTrue(
                (materialized / "data/v5_2_qd/historical/raw").is_dir()
            )
            self.assertFalse(
                (
                    materialized
                    / "data/v5_2_qd/historical_superseding_565534/raw"
                ).exists()
            )
            self.assertEqual(
                stale_before, hashlib.sha256(stale_schema.read_bytes()).hexdigest()
            )
        self.assertEqual(
            before,
            {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
        )

    def test_source_authority_materializes_current_tree_without_historical_parent_blob(
        self,
    ) -> None:
        import tests.v52_recovery_schedule_conflict_fixture as fixture

        from tests.v52_recovery_schedule_conflict_fixture import (
            matching_source_authority_root,
        )

        evidence = CONFIGURED_SOURCE_AUTHORITY_ROOT
        protected = _schedule_feed_evidence_files(evidence)
        self.assertTrue(protected)
        for namespace in (
            Path("data/v5_2_qd/historical"),
            Path("data/v5_2_qd/historical_superseding_565534"),
        ):
            self.assertTrue(
                any(path.is_relative_to(evidence / namespace) for path in protected)
            )
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
        }
        checkout, head, blob = _current_tree_checkout_with_missing_parent_blob(ROOT)
        self.addCleanup(shutil.rmtree, checkout.parent, ignore_errors=True)
        full = checkout.parent / "full"
        full_clone = subprocess.run(
            [
                "git",
                "clone",
                "--no-local",
                "--no-tags",
                checkout.as_uri(),
                str(full),
            ],
            cwd=checkout.parent,
            check=False,
            capture_output=True,
            text=True,
            env={
                "HOME": str(checkout.parent / "full-home"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        self.assertNotEqual(full_clone.returncode, 0)
        self.assertIn(blob, full_clone.stdout + full_clone.stderr)

        clone_calls: list[tuple[list[str], dict[str, object]]] = []
        delegate = fixture._git_run

        def recording_git_run(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[object]:
            if arguments and arguments[0] == "clone":
                clone_calls.append((arguments, kwargs))
            return delegate(arguments, **kwargs)  # type: ignore[arg-type]

        with patch.object(fixture, "_git_run", side_effect=recording_git_run):
            materialized = matching_source_authority_root(checkout, evidence)

        self.assertEqual(len(clone_calls), 1)
        clone_args = clone_calls[0][0]
        for flag in (
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--single-branch",
            "--depth=1",
            "-q",
        ):
            self.assertEqual(clone_args.count(flag), 1)
        self.assertIn(checkout.as_uri(), clone_args)
        self.assertEqual(Path(clone_args[-1]), materialized)
        self.assertTrue(materialized.is_dir())

        def git_text(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=materialized,
                check=True,
                capture_output=True,
                text=True,
                env={
                    "HOME": str(checkout.parent / "inspect-home"),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
            return completed.stdout.strip()

        self.assertEqual(git_text("rev-parse", "HEAD"), head)
        self.assertEqual(git_text("rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(git_text("rev-list", "--count", "HEAD"), "1")
        detached = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=materialized,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(detached.returncode, 0)
        self.assertFalse((materialized / ".git/objects/info/alternates").exists())
        refs = git_text("for-each-ref", "--format=%(refname) %(objectname)").splitlines()
        self.assertNotIn("refs/tags/fixture-intended-tip", "\n".join(refs))
        self.assertNotIn("refs/heads/fixture-unrelated", "\n".join(refs))
        self.assertTrue(refs)
        self.assertTrue(all(line.split(maxsplit=1)[1] == head for line in refs))
        object_root = materialized / ".git/objects"
        object_files = [path for path in object_root.rglob("*") if path.is_file()]
        self.assertTrue(object_files)
        self.assertTrue(all(path.stat().st_nlink == 1 for path in object_files))
        self.assertEqual(
            before,
            {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
        )

    def test_recovery_authority_fixture_ignores_hostile_git_environment_and_hooks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            side_effect = root / "hook-side-effect"
            hooks = root / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text(f"#!/bin/sh\ntouch {side_effect}\n", encoding="utf-8")
            hook.chmod(0o755)
            config = root / "hostile.gitconfig"
            config.write_text(
                f"[core]\n\thooksPath = {hooks}\n[init]\n\ttemplateDir = {hooks}\n",
                encoding="utf-8",
            )
            protected = tuple(
                path
                for path in RECOVERY_PARENT_EVIDENCE_ROOT.glob(
                    "data/v5_2_qd/**/mlb_schedule_game_feed/**/*"
                )
                if path.is_file()
            )
            before = {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
            }
            with patch.dict(
                os.environ,
                {
                    "HOME": str(root / "hostile-home"),
                    "GIT_CONFIG_GLOBAL": str(config),
                    "GIT_TEMPLATE_DIR": str(hooks),
                    "GIT_ASKPASS": "/bin/false",
                },
                clear=False,
            ):
                recovery_authority_fixture(
                    root / "fixture",
                    parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT,
                )
            self.assertFalse(side_effect.exists())
            self.assertEqual(
                before,
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in protected
                },
            )

class RecoveryDerivedEvidenceTests(unittest.TestCase):
    def test_catalog_loader_requires_all_three_published_bindings(self) -> None:
        from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionAuthority,
            build_recovery_schedule_conflict_catalog,
            load_recovery_disposition_bindings,
        )
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        with tempfile.TemporaryDirectory() as directory:
            repository, run_spec, contract = recovery_authority_fixture(
                Path(directory), parent_evidence_root=RECOVERY_PARENT_EVIDENCE_ROOT
            )
            catalog = build_recovery_schedule_conflict_catalog(
                repository, run_spec, contract
            )
            evidence_parent = Path(directory).resolve() / "evidence"
            evidence_parent.mkdir()
            store = V52EvidenceStore(evidence_parent / "raw")
            isolated = dataclasses.replace(
                catalog,
                entries=tuple(
                    dataclasses.replace(entry, evidence_root=store.root)
                    for entry in catalog.entries
                ),
            )
            with patch.object(
                RecoveryDispositionAuthority,
                "reauthenticate",
                autospec=True,
                side_effect=lambda authority: authority,
            ):
                with self.assertRaisesRegex(
                    (RuntimeError, ValueError), "success|binding"
                ):
                    load_recovery_disposition_bindings(isolated)
                for authority in isolated.entries:
                    semantics = recompute_query_sidecar(
                        authority.query, b"", authority=authority
                    )
                    with store.acquire_query(authority.query):
                        published = store.publish_derived_disposition(
                            authority.query, semantics, authority=authority
                        )
                    self.assertIsNone(published.raw_sha256)
                    folder = (
                        store.root
                        / "queries"
                        / authority.query.source
                        / logical_query_id(authority.query)
                    )
                    self.assertEqual(list((folder / "attempts").iterdir()), [])
                    self.assertEqual(list((folder / "objects").iterdir()), [])
                    with store.acquire_query(authority.query):
                        retried = store.publish_derived_disposition(
                            authority.query, semantics, authority=authority
                        )
                    self.assertEqual(retried, published)
                bindings = load_recovery_disposition_bindings(isolated)
                self.assertEqual(
                    tuple(binding.query_id for binding in bindings), isolated.query_ids()
                )


if __name__ == "__main__":
    unittest.main()
