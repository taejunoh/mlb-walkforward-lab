"""Closed, raw-less disposition for the one quarantined 2019 schedule pair.

This is deliberately not a schedule-parser policy.  It authenticates the old
quarantine evidence and permits precisely one removal before asking the
unchanged parser to reconcile the remaining ordinary duplicates.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .diagnostics import deep_freeze
from .protocols import SourceRunSpec
from .source_parsers.schedule import _normalize_game, parse_schedule
from .v52_evidence import LogicalQuery, PublishedEvidence, V52EvidenceStore
from .v52_query_plan import _load_static_contracts
from .v52_source_contracts import canonical_json_bytes


DISPOSITION_ENDPOINT = "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534"
OLD_QUERY_ID = "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8"
OLD_REGISTRATION_SHA256 = "5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95"
OLD_QUARANTINE_SHA256 = "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8"
OLD_RAW_SHA256 = "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0"
OLD_RAW_BYTE_COUNT = 21182105
OLD_RUN_SPEC_SHA256 = "d622bff184f0bdade22848d4b76fc43bdf9ac88c7a51513dee4a7d5f51bd1492"
OLD_ACQUISITION_CONTRACT_SHA256 = "cef89a1d4191ef83eb0fc2b0f4bdd191aeb8ba53cbb1bc6df73199604f23a9d9"
OLD_STATIC_BATCH_ID = "069fd746f9c7ecb1267b7ae44f8d44a1aeb3575a92c64482dacbc5b61d8f0571"
OLD_STATIC_BATCH_SHA256 = "a6a2ac816620797b21e5bcf297fb92453f1143ed0d3d4c01effbf15c531ca39a"
OLD_CHECKPOINT_SHA256 = "00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06"
SUPPRESSED_POSITION = (54, 5)
RETAINED_POSITION = (161, 12)
SUPPRESSED_OBJECT_SHA256 = "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941"
RETAINED_OBJECT_SHA256 = "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6"
NORMALIZED_GAMES_SHA256 = "a6089e74246b919b59519b93743a0281834ef759347faa67f457d5deb5191930"
SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256 = "114d3593ebee215b357622ee712011314061d0ded3e888d847dc90270a25a360"

SUPPRESSED_OCCURRENCE = MappingProxyType(
    {
        "date_index": 54,
        "game_index": 5,
        "sha256": SUPPRESSED_OBJECT_SHA256,
        "game_date": "2019-05-19T17:10:00Z",
        "venue_id": 2394,
        "calendar_event_id": "14-565534-2019-05-19",
        "resume_date": "2019-09-07T00:15:00Z",
        "resume_game_date": "2019-09-06",
    }
)
RETAINED_OCCURRENCE = MappingProxyType(
    {
        "date_index": 161,
        "game_index": 12,
        "sha256": RETAINED_OBJECT_SHA256,
        "game_date": "2019-09-07T00:15:00Z",
        "venue_id": 10,
        "calendar_event_id": "14-565534-2019-09-06",
        "resumed_from": "2019-05-19T17:10:00Z",
        "resumed_from_date": "2019-05-19",
    }
)

OLD_REGISTRATION_RELATIVE = Path(
    "data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed"
) / OLD_QUERY_ID / "query.json"
OLD_QUARANTINE_RELATIVE = Path(
    "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed"
) / OLD_QUERY_ID / "000001.json"
OLD_RAW_RELATIVE = Path(
    "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed"
) / OLD_QUERY_ID / f"{OLD_RAW_SHA256}.raw"

EXPECTED_OCCURRENCE_COUNTS = MappingProxyType(
    {
        "raw_total_games": 2472,
        "raw_date_total_games": 2472,
        "raw_regular_occurrences": 2472,
        "raw_unique_game_pks": 2430,
        "raw_duplicate_game_pks": 42,
        "special_suppressed_occurrences": 1,
        "projected_occurrences": 2471,
        "ordinary_reconciled_duplicate_game_pks": 41,
        "normalized_unique_games": 2430,
    }
)
OLD_BINDINGS = MappingProxyType(
    {
        "run_id": "historical_walkforward_500",
        "run_spec_sha256": OLD_RUN_SPEC_SHA256,
        "acquisition_contract_sha256": OLD_ACQUISITION_CONTRACT_SHA256,
        "static_batch_id": OLD_STATIC_BATCH_ID,
        "static_batch_sha256": OLD_STATIC_BATCH_SHA256,
        "checkpoint_sha256": OLD_CHECKPOINT_SHA256,
        "query_id": OLD_QUERY_ID,
        "quarantine_receipt_sha256": OLD_QUARANTINE_SHA256,
        "raw_sha256": OLD_RAW_SHA256,
    }
)


@dataclass(frozen=True)
class _FileSnapshot:
    relative: Path
    sha256: str
    byte_count: int | None
    identity: tuple[int, int]


@dataclass(frozen=True)
class ScheduleOccurrenceProjection:
    occurrence_counts: Mapping[str, int]
    suppressed_position: tuple[int, int]
    retained_position: tuple[int, int]
    normalized_games: tuple[Mapping[str, object], ...]
    selected_raw_game_object_sha256s: tuple[Mapping[str, object], ...]
    site_flags: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class DuplicateGamePkDispositionAuthority:
    repository_root: Path
    evidence_root: Path
    source_run_spec_path: Path
    acquisition_contract_path: Path
    query: LogicalQuery
    old_raw_relative: Path
    _snapshots: tuple[_FileSnapshot, ...]

    def _read_old_payload(self) -> dict[str, object]:
        snapshots, raw = _authenticate_old_evidence(self.repository_root)
        if snapshots != self._snapshots:
            raise ValueError("old 565534 authority changed during disposition")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("old 565534 raw is not UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("old 565534 raw root must be an object")
        return payload

    def project_payload(
        self, payload: Mapping[str, object], *, expected_counts: Mapping[str, int] | None = EXPECTED_OCCURRENCE_COUNTS
    ) -> ScheduleOccurrenceProjection:
        return _project_payload(payload, expected_counts=expected_counts)

    def recompute(self) -> ScheduleOccurrenceProjection:
        return self.project_payload(self._read_old_payload())

    def reauthenticate(self) -> DuplicateGamePkDispositionAuthority:
        """Rebuild and bind every new and old authority before use."""
        rebound = build_duplicate_disposition_authority(
            self.repository_root,
            source_run_spec_path=self.source_run_spec_path,
            acquisition_contract_path=self.acquisition_contract_path,
        )
        if rebound != self:
            raise ValueError("565534 disposition authority changed during rebind")
        return rebound


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _safe_relative_path(relative: Path, label: str) -> tuple[str, ...]:
    if relative.is_absolute() or not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError(f"{label} path is not repository-relative")
    return relative.parts


def _read_regular_single_link(root: Path, relative: Path, label: str) -> bytes:
    parts = _safe_relative_path(relative, label)
    root_status = root.lstat()
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ValueError(f"{label} root is unsafe")
    current = root
    for part in parts[:-1]:
        current = current / part
        node = current.lstat()
        if stat.S_ISLNK(node.st_mode) or not stat.S_ISDIR(node.st_mode):
            raise ValueError(f"{label} has an unsafe ancestor")
    target = root.joinpath(*parts)
    node = target.lstat()
    if stat.S_ISLNK(node.st_mode) or not stat.S_ISREG(node.st_mode) or node.st_nlink != 1:
        raise ValueError(f"{label} must be a regular single-link file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        linked = target.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError(f"{label} identity differs")
        return os.read(descriptor, opened.st_size)
    finally:
        os.close(descriptor)


def _snapshot(
    root: Path,
    relative: Path,
    expected_sha256: str,
    byte_count: int | None,
    label: str,
) -> tuple[_FileSnapshot, bytes]:
    contents = _read_regular_single_link(root, relative, label)
    node = (root / relative).lstat()
    if hashlib.sha256(contents).hexdigest() != expected_sha256:
        raise ValueError(f"{label} sha256 differs")
    if byte_count is not None and len(contents) != byte_count:
        raise ValueError(f"{label} byte count differs")
    return (
        _FileSnapshot(
            relative,
            expected_sha256,
            byte_count,
            (node.st_dev, node.st_ino),
        ),
        contents,
    )


def _authenticate_old_evidence(
    root: Path,
) -> tuple[tuple[_FileSnapshot, ...], bytes]:
    registration, registration_bytes = _snapshot(
        root,
        OLD_REGISTRATION_RELATIVE,
        OLD_REGISTRATION_SHA256,
        None,
        "old query registration",
    )
    quarantine, quarantine_bytes = _snapshot(
        root,
        OLD_QUARANTINE_RELATIVE,
        OLD_QUARANTINE_SHA256,
        None,
        "old quarantine receipt",
    )
    raw, raw_bytes = _snapshot(
        root,
        OLD_RAW_RELATIVE,
        OLD_RAW_SHA256,
        OLD_RAW_BYTE_COUNT,
        "old quarantined raw",
    )
    registration_payload = json.loads(registration_bytes)
    quarantine_payload = json.loads(quarantine_bytes)
    if (
        registration_payload.get("source") != "mlb_schedule_game_feed"
        or registration_payload.get("grain") != "season_schedule_and_game"
        or registration_payload.get("endpoint") != "https://statsapi.mlb.com/api/v1/schedule"
        or quarantine_payload.get("query_id") != OLD_QUERY_ID
        or quarantine_payload.get("raw_sha256") != OLD_RAW_SHA256
        or quarantine_payload.get("body_sha256") != OLD_RAW_SHA256
        or quarantine_payload.get("byte_count") != OLD_RAW_BYTE_COUNT
    ):
        raise ValueError("old 565534 evidence binding differs")
    return (registration, quarantine, raw), raw_bytes


def _requested_range() -> Mapping[str, object]:
    old_authority = {
        "query_id": OLD_QUERY_ID,
        "query_registration": {"path": str(OLD_REGISTRATION_RELATIVE), "sha256": OLD_REGISTRATION_SHA256},
        "quarantine_receipt": {"path": str(OLD_QUARANTINE_RELATIVE), "sha256": OLD_QUARANTINE_SHA256},
        "raw": {"path": str(OLD_RAW_RELATIVE), "sha256": OLD_RAW_SHA256, "byte_count": OLD_RAW_BYTE_COUNT},
    }
    return {
        "year": 2019,
        "start_date": "2019-03-20",
        "end_date": "2019-09-29",
        "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
        "game_pk": 565534,
        "old_authority": old_authority,
        "suppressed_occurrence": {"date_index": 54, "game_index": 5, "sha256": SUPPRESSED_OBJECT_SHA256},
        "retained_occurrence": {"date_index": 161, "game_index": 12, "sha256": RETAINED_OBJECT_SHA256},
    }


def build_duplicate_disposition_authority(
    root: Path,
    *,
    source_run_spec_path: Path,
    acquisition_contract_path: Path,
) -> DuplicateGamePkDispositionAuthority:
    root = Path(root).resolve(strict=True)
    context = _load_static_contracts(source_run_spec_path, acquisition_contract_path)
    if context.repository_root != root or not isinstance(context.authority, SourceRunSpec):
        raise ValueError("565534 disposition requires the authenticated superseding authority")
    if DISPOSITION_ENDPOINT not in next(
        source["endpoints"]
        for source in context.contract["sources"]
        if source["name"] == "mlb_schedule_game_feed"
    ):
        raise ValueError("superseding contract omits the 565534 disposition endpoint")
    if context.contract.get("supersedes") != dict(OLD_BINDINGS):
        raise ValueError("superseding contract old bindings differ")
    snapshots, _ = _authenticate_old_evidence(root)
    query = LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint=DISPOSITION_ENDPOINT,
        parameters=MappingProxyType({}),
        requested_range=_requested_range(),
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=context.authority_sha256,
        acquisition_contract_sha256=context.acquisition_contract_sha256,
    )
    return DuplicateGamePkDispositionAuthority(
        repository_root=root,
        evidence_root=context.raw_root,
        source_run_spec_path=context.authority_path,
        acquisition_contract_path=context.acquisition_contract_path,
        query=query,
        old_raw_relative=OLD_RAW_RELATIVE,
        _snapshots=snapshots,
    )


def _require_special_game(game: Mapping[str, object], *, first: bool) -> None:
    teams = game.get("teams")
    if not isinstance(teams, dict) or not isinstance(teams.get("away"), dict) or not isinstance(teams.get("home"), dict):
        raise ValueError("565534 teams differ")
    away, home = teams["away"], teams["home"]
    away_team = away.get("team") if isinstance(away, dict) else None
    home_team = home.get("team") if isinstance(home, dict) else None
    status = game.get("status")
    if (
        game.get("gamePk") != 565534 or game.get("gameType") != "R" or game.get("season") != "2019"
        or game.get("officialDate") != "2019-05-19" or not isinstance(away_team, dict)
        or not isinstance(home_team, dict) or away_team.get("id") != 133 or home_team.get("id") != 116
        or not isinstance(status, dict) or status.get("detailedState") != "Final"
        or away.get("score") != 7 or home.get("score") != 3
        or away.get("isWinner") is not True or home.get("isWinner") is not False
    ):
        raise ValueError("565534 fixed game facts differ")
    if first:
        if (
            game.get("gameDate") != "2019-05-19T17:10:00Z"
            or game.get("resumeDate") != "2019-09-07T00:15:00Z"
            or game.get("resumeGameDate") != "2019-09-06"
            or game.get("calendarEventID") != "14-565534-2019-05-19"
            or not isinstance(game.get("venue"), dict) or game["venue"].get("id") != 2394
            or _canonical_sha256(game) != SUPPRESSED_OBJECT_SHA256
        ):
            raise ValueError("565534 suppressed occurrence differs")
    elif (
        game.get("gameDate") != "2019-09-07T00:15:00Z"
        or game.get("resumedFrom") != "2019-05-19T17:10:00Z"
        or game.get("resumedFromDate") != "2019-05-19"
        or game.get("calendarEventID") != "14-565534-2019-09-06"
        or not isinstance(game.get("venue"), dict) or game["venue"].get("id") != 10
        or _canonical_sha256(game) != RETAINED_OBJECT_SHA256
    ):
        raise ValueError("565534 retained occurrence differs")


def _parse_authenticated_filtered_schedule(
    payload: Mapping[str, object],
) -> list[dict]:
    filtered = copy.deepcopy(payload)
    try:
        dates = filtered["dates"]
        date_entry = dates[SUPPRESSED_POSITION[0]]
        games = date_entry["games"]
        del games[SUPPRESSED_POSITION[1]]
        date_entry["totalGames"] -= 1
        filtered["totalGames"] -= 1
        serialized = canonical_json_bytes(filtered)
        try:
            return parse_schedule(serialized)
        finally:
            del serialized
    finally:
        del filtered


def _project_payload(payload: Mapping[str, object], *, expected_counts: Mapping[str, int] | None) -> ScheduleOccurrenceProjection:
    if set(payload) < {"totalGames", "dates"} or type(payload.get("totalGames")) is not int or not isinstance(payload.get("dates"), list):
        raise ValueError("schedule projection root differs")
    dates = payload["dates"]
    occurrences: list[tuple[int, int, Mapping[str, object], Mapping[str, object]]] = []
    date_total = 0
    special: list[tuple[int, int, Mapping[str, object]]] = []
    for date_index, date_entry in enumerate(dates):
        if not isinstance(date_entry, dict) or type(date_entry.get("totalGames")) is not int or not isinstance(date_entry.get("games"), list):
            raise ValueError("schedule projection date differs")
        games = date_entry["games"]
        if date_entry["totalGames"] != len(games):
            raise ValueError("schedule projection date total differs")
        date_total += date_entry["totalGames"]
        for game_index, game in enumerate(games):
            if not isinstance(game, dict):
                raise ValueError("schedule projection game differs")
            if game.get("gameType") == "R":
                occurrences.append((date_index, game_index, date_entry, game))
                if game.get("gamePk") == 565534:
                    special.append((date_index, game_index, game))
    if len(special) != 2 or {(row[0], row[1]) for row in special} != {SUPPRESSED_POSITION, RETAINED_POSITION}:
        raise ValueError("565534 must have exactly its two authenticated occurrences")
    special_by_position = {(date_index, game_index): game for date_index, game_index, game in special}
    _require_special_game(special_by_position[SUPPRESSED_POSITION], first=True)
    _require_special_game(special_by_position[RETAINED_POSITION], first=False)
    game_pks = [game.get("gamePk") for _, _, _, game in occurrences]
    if not all(type(game_pk) is int for game_pk in game_pks):
        raise ValueError("regular schedule gamePk differs")
    duplicate_pks = {game_pk for game_pk in game_pks if game_pks.count(game_pk) == 2}
    if len(game_pks) != len(set(game_pks)) + len(duplicate_pks):
        raise ValueError("schedule duplicates are not exact pairs")
    projected = [row for row in occurrences if row[:2] != SUPPRESSED_POSITION]
    selected = _select_raw_occurrences(projected)
    selected_normalized = [game for game, _ in selected]
    ordinary_duplicates = len(duplicate_pks - {565534})
    counts = {
        "raw_total_games": payload["totalGames"],
        "raw_date_total_games": date_total,
        "raw_regular_occurrences": len(occurrences),
        "raw_unique_game_pks": len(set(game_pks)),
        "raw_duplicate_game_pks": len(duplicate_pks),
        "special_suppressed_occurrences": 1,
        "projected_occurrences": len(projected),
        "ordinary_reconciled_duplicate_game_pks": ordinary_duplicates,
        "normalized_unique_games": len(selected_normalized),
    }
    if expected_counts is not None and counts != dict(expected_counts):
        raise ValueError("565534 projection occurrence totals differ")
    normalized = _parse_authenticated_filtered_schedule(payload)
    if (
        normalized != selected_normalized
        or canonical_json_bytes(normalized) != canonical_json_bytes(selected_normalized)
    ):
        raise ValueError("565534 projection differs from unchanged parse_schedule")
    if _canonical_sha256(normalized) != NORMALIZED_GAMES_SHA256:
        raise ValueError("565534 projection differs from unchanged parse_schedule")
    selected_hashes = tuple(
        MappingProxyType({"game_pk": game["game_pk"], "raw_game_object_sha256": _canonical_sha256(raw_game)})
        for game, (_, _, _, raw_game) in selected
    )
    selected_rows = sorted(selected_hashes, key=lambda row: int(row["game_pk"]))
    if _canonical_sha256([dict(row) for row in selected_rows]) != SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256:
        raise ValueError("565534 selected raw-object digest differs")
    site_flags = tuple(MappingProxyType({"game_pk": game["game_pk"], "neutral_site": raw_game.get("neutralSite", False), "alternate_site": raw_game.get("isAlternateSite", False)}) for game, (_, _, _, raw_game) in selected)
    return ScheduleOccurrenceProjection(
        occurrence_counts=MappingProxyType(counts),
        suppressed_position=SUPPRESSED_POSITION,
        retained_position=RETAINED_POSITION,
        normalized_games=tuple(deep_freeze(game) for game in normalized),
        selected_raw_game_object_sha256s=selected_hashes,
        site_flags=site_flags,
    )


def _select_raw_occurrences(occurrences: list[tuple[int, int, Mapping[str, object], Mapping[str, object]]]) -> list[tuple[dict, tuple[int, int, Mapping[str, object], Mapping[str, object]]]]:
    selected: dict[int, tuple[dict, tuple[int, int, Mapping[str, object], Mapping[str, object]]]] = {}
    order: list[int] = []
    for flat_index, occurrence in enumerate(occurrences):
        _, _, date_entry, raw_game = occurrence
        if not isinstance(date_entry, dict) or not isinstance(raw_game, dict):
            raise ValueError("schedule projection occurrence differs")
        game = _normalize_game(raw_game, date_entry, flat_index)
        if game is None:
            continue
        game_pk = game["game_pk"]
        prior = selected.get(game_pk)
        if prior is None:
            selected[game_pk] = (game, occurrence)
            order.append(game_pk)
            continue
        prior_game = prior[0]
        fields = ("game_pk", "away_team_id", "away_team", "home_team_id", "home_team")
        if any(prior_game[field] != game[field] for field in fields):
            raise ValueError("malformed schedule: conflicting duplicate gamePk")
        if prior_game == game:
            continue
        played = {"Final", "Completed Early"}
        if prior_game["status"] in played and game["status"] in played:
            comparable = set(prior_game) | set(game)
            comparable.discard("scheduled_start_utc")
            if any(prior_game.get(field) != game.get(field) for field in comparable):
                raise ValueError("malformed schedule: conflicting duplicate gamePk")
            if game["scheduled_start_utc"] < prior_game["scheduled_start_utc"]:
                selected[game_pk] = (game, occurrence)
        elif game["status"] in played or prior_game["status"] not in played:
            selected[game_pk] = (game, occurrence)
    return [selected[game_pk] for game_pk in order]


def project_schedule_occurrences(authority: DuplicateGamePkDispositionAuthority) -> ScheduleOccurrenceProjection:
    if not isinstance(authority, DuplicateGamePkDispositionAuthority):
        raise TypeError("authority must be a DuplicateGamePkDispositionAuthority")
    return authority.recompute()


def _sidecar_semantics(authority: DuplicateGamePkDispositionAuthority, projection: ScheduleOccurrenceProjection) -> Mapping[str, object]:
    return {
        "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
        "game_pk": 565534,
        "old_authority": authority.query.requested_range["old_authority"],
        "fixed_game_facts": {"season": 2019, "official_date": "2019-05-19", "away_team_id": 133, "home_team_id": 116, "terminal_status": "Final", "away_score": 7, "home_score": 3, "winner_team_id": 133},
        "suppressed_occurrence": dict(SUPPRESSED_OCCURRENCE),
        "retained_occurrence": dict(RETAINED_OCCURRENCE),
        "occurrence_counts": dict(projection.occurrence_counts),
        "normalized_games_sha256": NORMALIZED_GAMES_SHA256,
        "selected_raw_game_object_sha256s_sha256": (
            SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256
        ),
    }


def publish_duplicate_gamepk_disposition(store: V52EvidenceStore, authority: DuplicateGamePkDispositionAuthority) -> PublishedEvidence:
    if not isinstance(store, V52EvidenceStore) or store.root != authority.evidence_root:
        raise ValueError("565534 disposition store root differs from authenticated authority")
    rebound = authority.reauthenticate()
    projection = rebound.recompute()
    with store.acquire_query(rebound.query):
        return store.publish_derived_disposition(
            rebound.query,
            _sidecar_semantics(rebound, projection),
            authority=rebound,
        )
