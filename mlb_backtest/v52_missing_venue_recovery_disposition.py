"""Sealed r3 dispositions for the 2021 missing-venue recovery record.

The r3 protocol is tracked in the active checkout, while the large immutable
parent responses live in a separately configured protected evidence root.  No
caller-supplied context, sidecar, or partial query list participates here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import deep_freeze
from .v52_evidence import LogicalQuery, canonical_query_bytes, logical_query_id
from .v52_query_plan import (
    R3_STATIC_QUERY_TABLE,
    QueryBatch,
    _load_static_contracts,
    build_static_batch,
)
from .v52_schedule_conflict_recovery_disposition import RecoveryDispositionProjection
from .source_parsers.schedule import _normalize_game, parse_schedule
from .v52_recovery_parent_evidence import (
    RecoveryParentCatalog,
    build_recovery_parent_catalog,
)
from .v52_seasons import SEASON_BOUNDARIES
from .v52_source_contracts import canonical_json_bytes, canonical_sha256


R3_RECOVERY_RUN_ID = (
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
)
R3_RUN_SPEC_RELATIVE = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
)
R3_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
)

_R3_RUN_SHA256 = "9b0a9dbbf9df8bd621ae4f74dfbe0854e601229cebe404c9bb72015724fdac38"
_R3_CONTRACT_SHA256 = "1e9b72738f8e1599d6c54f388aa09f274e83ff8a9e899bb494121ec3e7e1fd9d"
_R3_STATIC_BATCH_ID = "3f572f5d9b5bb1680da947c658e4828a4b883ca6e5b2176b338c7f8e6049824f"
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_PROJECTION_ENDPOINTS = (
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
    "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924",
)
_MISSING_VENUE_ENDPOINT = "disposition://MISSING_VENUE_632924"
_MISSING_VENUE_PARENT = "missing_venue_2021_632924"
_MISSING_VENUE_POSITION = (130, 10)
RECOVERY_PARSER_POLICY_VERSION = "v5.2.3"
_MISSING_VENUE_OBJECT_SHA256 = (
    "705969812dedbc3b7d1b3e4bbd6d877671b34657a96cc5b34b22d35d820261a4"
)
_PARENT_RECONCILED_KEY = "parent_" + "recomputed"
_RECORD_PAIRS = ((565534, 0), (630882, 1), (633224, 2), (632924, 2))
_RECORD_ENDPOINTS = (
    _PROJECTION_ENDPOINTS[0],
    _PROJECTION_ENDPOINTS[1],
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
    _MISSING_VENUE_ENDPOINT,
)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _frozen_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    frozen = deep_freeze(_plain(value))
    if not isinstance(frozen, Mapping):  # pragma: no cover - deep_freeze invariant
        raise AssertionError(f"{label} did not freeze as a mapping")
    return frozen


def _canonical_mapping_bytes(value: object, label: str) -> bytes:
    return canonical_json_bytes(_plain(_frozen_mapping(value, label)))


def _decode_mapping_bytes(value: bytes, label: str) -> Mapping[str, object]:
    if not isinstance(value, bytes):
        raise ValueError(f"{label} bytes differ")
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} bytes differ") from exc
    if canonical_json_bytes(decoded) != value:
        raise ValueError(f"{label} bytes differ")
    return _frozen_mapping(decoded, label)


def _query_from_canonical_bytes(value: bytes) -> LogicalQuery:
    record = _decode_mapping_bytes(value, "r3 projection query")
    expected = {
        "acquisition_contract_sha256",
        "endpoint",
        "expected_media_type",
        "grain",
        "parameters",
        "parent_query_id",
        "parser_version",
        "requested_range",
        "run_spec_sha256",
        "source",
    }
    if set(record) != expected:
        raise ValueError("r3 projection query bytes differ")
    parameters = _frozen_mapping(record["parameters"], "r3 projection parameters")
    requested_range = _frozen_mapping(
        record["requested_range"], "r3 projection requested range"
    )
    fields = (
        "source",
        "grain",
        "endpoint",
        "expected_media_type",
        "parser_version",
        "run_spec_sha256",
        "acquisition_contract_sha256",
    )
    if (
        any(not isinstance(record[field], str) for field in fields)
        or record["parent_query_id"] is not None
    ):
        raise ValueError("r3 projection query bytes differ")
    query = LogicalQuery(
        source=record["source"],
        grain=record["grain"],
        endpoint=record["endpoint"],
        parameters=parameters,
        requested_range=requested_range,
        expected_media_type=record["expected_media_type"],
        parser_version=record["parser_version"],
        parent_query_id=None,
        run_spec_sha256=record["run_spec_sha256"],
        acquisition_contract_sha256=record["acquisition_contract_sha256"],
    )
    if canonical_query_bytes(query) != value:
        raise ValueError("r3 projection query bytes differ")
    return query


def _expected_static_query(
    index: int, run_sha: str, contract_sha: str
) -> tuple[int, str, Mapping[str, object], Mapping[str, object]]:
    try:
        year, endpoint, parser = R3_STATIC_QUERY_TABLE[index]
    except IndexError as exc:  # pragma: no cover - closed table below
        raise ValueError("r3 static query order differs") from exc
    if parser != RECOVERY_PARSER_POLICY_VERSION:
        raise AssertionError("r3 parser policy table differs")
    first, last = SEASON_BOUNDARIES[year]
    parameters: Mapping[str, object]
    if endpoint.startswith("disposition://"):
        parameters = {}
    else:
        parameters = {
            "sportId": 1,
            "gameType": "R",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "hydrate": "probablePitcher,lineups,venue,linescore",
        }
    return (
        year,
        endpoint,
        parameters,
        {
            "year": year,
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        },
    )


def _validate_static_query(
    query: object, index: int, run_sha: str, contract_sha: str
) -> LogicalQuery:
    if not isinstance(query, LogicalQuery):
        raise ValueError("r3 static query type differs")
    if not isinstance(query.parameters, Mapping) or not isinstance(
        query.requested_range, Mapping
    ):
        raise ValueError("r3 static query mapping differs")
    year, endpoint, parameters, requested_range = _expected_static_query(
        index, run_sha, contract_sha
    )
    if (
        query.source != "mlb_schedule_game_feed"
        or query.grain != "season_schedule_and_game"
        or query.endpoint != endpoint
        or dict(query.parameters) != parameters
        or dict(query.requested_range) != requested_range
        or query.expected_media_type != "application/json"
        or query.parser_version != RECOVERY_PARSER_POLICY_VERSION
        or query.parent_query_id is not None
        or query.run_spec_sha256 != run_sha
        or query.acquisition_contract_sha256 != contract_sha
        or query.requested_range.get("year") != year
    ):
        raise ValueError("r3 static query identity differs")
    return query


def _validate_r3_static_batch(
    batch: object, expected_run_sha: str, expected_contract_sha: str
) -> tuple[LogicalQuery, ...]:
    """Validate every r3 static query before selecting the three projections."""
    if (
        expected_run_sha != _R3_RUN_SHA256
        or expected_contract_sha != _R3_CONTRACT_SHA256
    ):
        raise ValueError("r3 static authority digest differs")
    if not isinstance(batch, QueryBatch):
        raise ValueError("r3 static batch type differs")
    if (
        batch.stage != "STATIC_SCHEDULES"
        or batch.derived_from_sha256 is not None
        or batch.batch_id != _R3_STATIC_BATCH_ID
        or not isinstance(batch.queries, tuple)
        or len(batch.queries) != 7
    ):
        raise ValueError("r3 static batch identity differs")
    queries = tuple(
        _validate_static_query(query, index, expected_run_sha, expected_contract_sha)
        for index, query in enumerate(batch.queries)
    )
    ids = tuple(logical_query_id(query) for query in queries)
    if len(set(ids)) != 7:
        raise ValueError("r3 static batch contains duplicate query identities")
    if (
        tuple(
            query.endpoint
            for query in queries
            if query.endpoint.startswith("disposition://")
        )
        != _PROJECTION_ENDPOINTS
    ):
        raise ValueError("r3 static projection endpoints differ")
    if any(query.endpoint == _MISSING_VENUE_ENDPOINT for query in queries):
        raise ValueError("r3 missing-venue query must share the 2021 projection")
    return queries


def _missing_venue_raw_object(raw: bytes) -> Mapping[str, object]:
    """Purely validate the fixed raw position; callers authenticate ``raw`` first."""
    try:
        payload = json.loads(raw)
        date_index, game_index = _MISSING_VENUE_POSITION
        game = payload["dates"][date_index]["games"][game_index]
        if not isinstance(game, Mapping):
            raise ValueError
        no_lf_digest = hashlib.sha256(canonical_json_bytes(_plain(game))).hexdigest()
        lf_digest = hashlib.sha256(
            canonical_json_bytes(_plain(game)) + b"\n"
        ).hexdigest()
        teams = game.get("teams")
        away = teams.get("away") if isinstance(teams, Mapping) else None
        home = teams.get("home") if isinstance(teams, Mapping) else None
        away_team = away.get("team") if isinstance(away, Mapping) else None
        home_team = home.get("team") if isinstance(home, Mapping) else None
        status = game.get("status")
        if (
            no_lf_digest != _MISSING_VENUE_OBJECT_SHA256
            or lf_digest == _MISSING_VENUE_OBJECT_SHA256
            or game.get("gameType") != "R"
            or game.get("gamePk") != 632924
            or game.get("officialDate") != "2021-08-12"
            or game.get("gameDate") != "2021-08-12T23:00:00Z"
            or game.get("calendarEventID") != "14-632924-2021-08-12"
            or game.get("venue") != {"link": "/api/v1/venues/null"}
            or not isinstance(status, Mapping)
            or status.get("detailedState") != "Final"
            or not isinstance(away_team, Mapping)
            or not isinstance(home_team, Mapping)
            or away_team.get("id") != 147
            or away_team.get("name") != "New York Yankees"
            or away.get("score") != 8
            or away.get("isWinner") is not False
            or home_team.get("id") != 145
            or home_team.get("name") != "Chicago White Sox"
            or home.get("score") != 9
            or home.get("isWinner") is not True
        ):
            raise ValueError
        return _frozen_mapping(game, "missing-venue raw object")
    except (
        IndexError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise ValueError("missing-venue raw object differs") from exc


_EXPECTED_MISSING_VENUE_PARENT_BINDING = {
    "query_id": "91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2",
    "registration": {
        "path": "data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/query.json",
        "sha256": "b915233fb3c3ea796330486490f3b90d6746ce82e1f03b30b9c964ba6569cfd3",
        "byte_count": 675,
    },
    "attempt": {
        "path": "data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/attempts/000001.json",
        "sha256": "b7d76d96191bc80c578965ec5242a3abeb44dade601a5ca3c76c30d4c45f1652",
        "byte_count": 664,
    },
    "quarantine_receipt": {
        "path": "data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/000001.json",
        "sha256": "d3c9d9f197cab51d5ab509887002e4df3f300cb20c23a888120395e9b7042c36",
        "byte_count": 938,
    },
    "raw": {
        "path": "data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17.raw",
        "sha256": "84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17",
        "byte_count": 21223271,
    },
}


@dataclass(frozen=True)
class _R3ConflictAuthority:
    game_pk: int
    parent_name: str
    suppressed_positions: tuple[tuple[int, int], ...]
    retained_position: tuple[int, int]
    counts: Mapping[str, int]
    fixed_game_facts: Mapping[str, object]
    suppressed_occurrences: tuple[Mapping[str, object], ...]
    retained_occurrence: Mapping[str, object]
    normalized_games_sha256: str
    selected_raw_game_object_sha256s_sha256: str
    played_date: str | None = None
    result_available_on: str | None = None


def _authority(
    game_pk: int,
    parent_name: str,
    suppressed_positions: tuple[tuple[int, int], ...],
    retained_position: tuple[int, int],
    counts: Mapping[str, int],
    fixed: Mapping[str, object],
    suppressed: tuple[Mapping[str, object], ...],
    retained: Mapping[str, object],
    normalized: str,
    selected: str,
    played: str | None = None,
) -> _R3ConflictAuthority:
    return _R3ConflictAuthority(
        game_pk,
        parent_name,
        suppressed_positions,
        retained_position,
        _frozen_mapping(counts, "r3 conflict counts"),
        _frozen_mapping(fixed, "r3 fixed facts"),
        tuple(_frozen_mapping(item, "r3 suppressed occurrence") for item in suppressed),
        _frozen_mapping(retained, "r3 retained occurrence"),
        normalized,
        selected,
        played,
        played,
    )


_R3_CONFLICT_AUTHORITY = (
    _authority(
        565534,
        "original_2019_565534",
        ((54, 5),),
        (161, 12),
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
        },
        {
            "season": 2019,
            "official_date": "2019-05-19",
            "away_team_id": 133,
            "home_team_id": 116,
            "terminal_status": "Final",
            "away_score": 7,
            "home_score": 3,
            "winner_team_id": 133,
        },
        (
            {
                "date_index": 54,
                "game_index": 5,
                "sha256": "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941",
                "game_date": "2019-05-19T17:10:00Z",
                "venue_id": 2394,
                "calendar_event_id": "14-565534-2019-05-19",
                "resume_date": "2019-09-07T00:15:00Z",
                "resume_game_date": "2019-09-06",
            },
        ),
        {
            "date_index": 161,
            "game_index": 12,
            "sha256": "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6",
            "game_date": "2019-09-07T00:15:00Z",
            "venue_id": 10,
            "calendar_event_id": "14-565534-2019-09-06",
            "resumed_from": "2019-05-19T17:10:00Z",
            "resumed_from_date": "2019-05-19",
        },
        "a6089e74246b919b59519b93743a0281834ef759347faa67f457d5deb5191930",
        "114d3593ebee215b357622ee712011314061d0ded3e888d847dc90270a25a360",
    ),
    _authority(
        630882,
        "superseding_2020_630882",
        ((17, 0),),
        (22, 0),
        {
            "raw_total_games": 973,
            "raw_date_total_games": 973,
            "raw_regular_occurrences": 973,
            "raw_unique_game_pks": 900,
            "raw_duplicate_game_pks": 71,
            "raw_duplicate_member_occurrences": 144,
            "raw_duplicate_extra_occurrences": 73,
            "special_suppressed_occurrences": 1,
            "projected_occurrences": 972,
            "normalized_unique_games": 900,
        },
        {
            "season": 2020,
            "official_date": "2020-08-09",
            "away_team_id": 110,
            "home_team_id": 120,
            "terminal_status": "Final",
            "away_score": 6,
            "home_score": 2,
            "winner_team_id": 110,
        },
        (
            {
                "date_index": 17,
                "game_index": 0,
                "sha256": "2a5b251745546e643897cfc01eaa4c63c949bb8d8243b7fec44d3a23484f9be4",
                "game_date": "2020-08-09T16:35:00Z",
                "venue_id": 3309,
                "resume_date": "2020-08-14T21:05:00Z",
                "resume_game_date": "2020-08-14",
            },
        ),
        {
            "date_index": 22,
            "game_index": 0,
            "sha256": "0d961f12abb78e3b833c9f5c5bbbff76cad80a5370f57786904c7b42e82b204e",
            "game_date": "2020-08-14T21:05:00Z",
            "venue_id": 2,
            "resumed_from": "2020-08-09T16:35:00Z",
            "resumed_from_date": "2020-08-09",
        },
        "536d5b7e52e8ce3445abd31c3bbac4ba1ec82b44c987accd8c1d428ef139f8d9",
        "54c39b5be026844e5b8bb1fe2058fece1eee75f634d2c8c2f2f96cf331f494b1",
        "2020-08-14",
    ),
    _authority(
        633224,
        "superseding_2021_633224",
        ((106, 12), (108, 2)),
        (173, 11),
        {
            "raw_total_games": 2512,
            "raw_date_total_games": 2512,
            "raw_regular_occurrences": 2512,
            "raw_unique_game_pks": 2430,
            "raw_duplicate_game_pks": 80,
            "raw_duplicate_member_occurrences": 162,
            "raw_duplicate_extra_occurrences": 82,
            "special_suppressed_occurrences": 2,
            "projected_occurrences": 2510,
            "normalized_unique_games": 2430,
        },
        {
            "season": 2021,
            "official_date": "2021-07-21",
            "away_team_id": 135,
            "home_team_id": 144,
            "terminal_status": "Final",
            "away_score": 6,
            "home_score": 5,
            "winner_team_id": 135,
        },
        (
            {
                "date_index": 106,
                "game_index": 12,
                "sha256": "b5d52997a362f89e669e1454960b5230d4977f30370ad3f3ad7ca1b12be5221d",
                "game_date": "2021-07-19T23:20:00Z",
                "venue_id": 4705,
                "terminal_status": "Postponed",
            },
            {
                "date_index": 108,
                "game_index": 2,
                "sha256": "25f8ab0769219c592779cd800bb2b3278c9c75644647bd51ae5db3051bcf5a61",
                "game_date": "2021-07-21T21:20:00Z",
                "venue_id": 4705,
                "terminal_status": "Final",
                "resume_date": "2021-09-25T00:10:00Z",
                "resume_game_date": "2021-09-24",
            },
        ),
        {
            "date_index": 173,
            "game_index": 11,
            "sha256": "109a5d145731938e08f516f19345557894316b3a72687f4ac63029df07afe94c",
            "game_date": "2021-09-25T00:10:00Z",
            "venue_id": 2680,
            "terminal_status": "Final",
            "resumed_from": "2021-07-21T21:20:00Z",
            "resumed_from_date": "2021-07-21",
        },
        "891f73f68c3108ccf78f11f0ee11125bd6fdb4dc71e17bddadbcb8d88c008769",
        "17362576e0ae9e96a0e20979a9f8afdfbb95de03e2aa3fd4d66a4aa63486681f",
        "2021-09-24",
    ),
)


_R3_PARENT_BINDING_TEMPLATES: Mapping[str, Mapping[str, object]] = {
    "original_2019_565534": {
        "query_id": "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8",
        "registration": {
            "path": "data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/query.json",
            "sha256": "5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95",
            "byte_count": 675,
        },
        "quarantine": {
            "path": "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/000001.json",
            "sha256": "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8",
            "byte_count": 938,
        },
        "raw": {
            "path": "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0.raw",
            "sha256": "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0",
            "byte_count": 21182105,
        },
    },
    "superseding_2020_630882": {
        "query_id": "8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816",
        "registration": {
            "path": "data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/query.json",
            "sha256": "e5af77c4750d9de564d6a7203564b30056a89627f70c868e7ff0a1753d674106",
            "byte_count": 675,
        },
        "attempt": {
            "path": "data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/attempts/000001.json",
            "sha256": "fe37c66d26cc435d1375432b4f31c4b21546f28e46648de6379eeb67b1c527e2",
            "byte_count": 662,
        },
        "quarantine": {
            "path": "data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/000001.json",
            "sha256": "47bd466897b87b23a3c625737c0cae5aad1475491fdee93454489df9c38752cc",
            "byte_count": 936,
        },
        "raw": {
            "path": "data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b.raw",
            "sha256": "b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b",
            "byte_count": 7898917,
        },
    },
    "superseding_2021_633224": {
        "query_id": _EXPECTED_MISSING_VENUE_PARENT_BINDING["query_id"],
        "registration": _EXPECTED_MISSING_VENUE_PARENT_BINDING["registration"],
        "attempt": _EXPECTED_MISSING_VENUE_PARENT_BINDING["attempt"],
        "quarantine": _EXPECTED_MISSING_VENUE_PARENT_BINDING["quarantine_receipt"],
        "raw": _EXPECTED_MISSING_VENUE_PARENT_BINDING["raw"],
    },
}


def _catalog_parent_binding_template(
    authority: _R3ConflictAuthority,
) -> Mapping[str, object]:
    try:
        return _frozen_mapping(
            _R3_PARENT_BINDING_TEMPLATES[authority.parent_name],
            "r3 conflict parent template",
        )
    except KeyError as exc:
        raise ValueError("r3 conflict parent identity differs") from exc


def _catalog_parent_binding(
    catalog: RecoveryParentCatalog, authority: _R3ConflictAuthority
) -> Mapping[str, object]:
    parent = catalog.parents.get(authority.parent_name)
    if parent is None:
        raise ValueError("r3 conflict parent identity differs")
    values: dict[str, object] = {
        "query_id": parent.query_id,
        "registration": {
            "path": parent.registration.relative.as_posix(),
            "sha256": parent.registration.sha256,
            "byte_count": parent.registration.byte_count,
        },
        "quarantine": {
            "path": parent.quarantine.relative.as_posix(),
            "sha256": parent.quarantine.sha256,
            "byte_count": parent.quarantine.byte_count,
        },
        "raw": {
            "path": parent.raw.relative.as_posix(),
            "sha256": parent.raw.sha256,
            "byte_count": parent.raw.byte_count,
        },
    }
    if parent.attempt is not None:
        values["attempt"] = {
            "path": parent.attempt.relative.as_posix(),
            "sha256": parent.attempt.sha256,
            "byte_count": parent.attempt.byte_count,
        }
    actual = _frozen_mapping(values, "r3 conflict parent binding")
    if actual != _catalog_parent_binding_template(authority):
        raise ValueError("r3 conflict parent binding differs")
    return actual


def _missing_venue_parent_binding(
    parent_catalog: RecoveryParentCatalog,
) -> Mapping[str, object]:
    authority = _R3_CONFLICT_AUTHORITY[2]
    binding = dict(_plain(_catalog_parent_binding(parent_catalog, authority)))
    binding["quarantine_receipt"] = binding.pop("quarantine")
    if binding != _EXPECTED_MISSING_VENUE_PARENT_BINDING:
        raise ValueError("missing-venue parent binding differs")
    return _frozen_mapping(binding, "missing-venue parent binding")


def _expected_conflict_semantics(
    authority: _R3ConflictAuthority,
    query: LogicalQuery,
    parent_binding: Mapping[str, object],
) -> Mapping[str, object]:
    values: dict[str, object] = {
        "game_pk": authority.game_pk,
        "projection_query_endpoint": query.endpoint,
        _PARENT_RECONCILED_KEY: True,
        "parent_evidence": parent_binding,
        "fixed_game_facts": authority.fixed_game_facts,
        "retained_occurrence": authority.retained_occurrence,
        "occurrence_counts": authority.counts,
        "normalized_games_sha256": authority.normalized_games_sha256,
        "selected_raw_game_object_sha256s_sha256": authority.selected_raw_game_object_sha256s_sha256,
        "retained_scheduled_start_utc": authority.retained_occurrence["game_date"],
    }
    if len(authority.suppressed_occurrences) == 1:
        values["suppressed_occurrence"] = authority.suppressed_occurrences[0]
    else:
        values["suppressed_occurrences"] = authority.suppressed_occurrences
    if authority.played_date is not None:
        values["played_date"] = authority.played_date
        values["result_available_on"] = authority.result_available_on
    return _frozen_mapping(values, "r3 conflict semantics")


def _validate_record_content(
    game_pk: int,
    projection_index: int,
    query_bytes: bytes,
    semantics_bytes: bytes,
) -> None:
    if (game_pk, projection_index) not in _RECORD_PAIRS:
        raise ValueError("r3 disposition record identity differs")
    query = _query_from_canonical_bytes(query_bytes)
    expected_query = _validate_static_query(
        query, (1, 2, 3)[projection_index], _R3_RUN_SHA256, _R3_CONTRACT_SHA256
    )
    if query != expected_query:
        raise ValueError("r3 disposition record query differs")
    semantics = _decode_mapping_bytes(semantics_bytes, "r3 disposition semantics")
    if game_pk in {565534, 630882, 633224}:
        authority = _R3_CONFLICT_AUTHORITY[projection_index]
        expected = _expected_conflict_semantics(
            authority, query, _catalog_parent_binding_template(authority)
        )
    else:
        raw = semantics.get("raw_object")
        if (
            not isinstance(raw, Mapping)
            or hashlib.sha256(canonical_json_bytes(_plain(raw))).hexdigest()
            != _MISSING_VENUE_OBJECT_SHA256
        ):
            raise ValueError("r3 missing-venue semantics differ")
        expected = {
            "game_pk": 632924,
            "excluded_game_pks": [632924],
            "raw_object_sha256": _MISSING_VENUE_OBJECT_SHA256,
            "raw_object": raw,
            "parent_binding": _EXPECTED_MISSING_VENUE_PARENT_BINDING,
            "shared_2021_conflict_semantics": _expected_conflict_semantics(
                _R3_CONFLICT_AUTHORITY[2],
                query,
                _catalog_parent_binding_template(_R3_CONFLICT_AUTHORITY[2]),
            ),
        }
    if _plain(semantics) != _plain(expected):
        raise ValueError("r3 disposition semantics differ")


@dataclass(frozen=True, init=False)
class MissingVenueDispositionRecord:
    """A sealed record retaining only canonical query and semantics bytes."""

    game_pk: int
    projection_index: int
    _query_canonical_bytes: bytes
    _semantics_canonical_bytes: bytes

    def __init__(
        self,
        game_pk: int,
        projection_index: int,
        query: LogicalQuery,
        semantics: Mapping[str, object],
    ) -> None:
        query_bytes = canonical_query_bytes(query)
        semantics_bytes = _canonical_mapping_bytes(
            semantics, "r3 disposition semantics"
        )
        _validate_record_content(
            game_pk, projection_index, query_bytes, semantics_bytes
        )
        object.__setattr__(self, "game_pk", game_pk)
        object.__setattr__(self, "projection_index", projection_index)
        object.__setattr__(self, "_query_canonical_bytes", query_bytes)
        object.__setattr__(self, "_semantics_canonical_bytes", semantics_bytes)

    @classmethod
    def from_canonical_bytes(
        cls,
        game_pk: int,
        projection_index: int,
        query_canonical_bytes: bytes,
        semantics_canonical_bytes: bytes,
    ) -> MissingVenueDispositionRecord:
        """Construct from transportable bytes only after exact template validation."""
        _validate_record_content(
            game_pk,
            projection_index,
            query_canonical_bytes,
            semantics_canonical_bytes,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "game_pk", game_pk)
        object.__setattr__(instance, "projection_index", projection_index)
        object.__setattr__(instance, "_query_canonical_bytes", query_canonical_bytes)
        object.__setattr__(
            instance, "_semantics_canonical_bytes", semantics_canonical_bytes
        )
        return instance

    @staticmethod
    def _decode_query_bytes(value: bytes) -> LogicalQuery:
        return _query_from_canonical_bytes(value)

    @property
    def query(self) -> LogicalQuery:
        query = self._decode_query_bytes(self._query_canonical_bytes)
        _validate_record_content(
            self.game_pk,
            self.projection_index,
            self._query_canonical_bytes,
            self._semantics_canonical_bytes,
        )
        return query

    @property
    def semantics(self) -> Mapping[str, object]:
        _validate_record_content(
            self.game_pk,
            self.projection_index,
            self._query_canonical_bytes,
            self._semantics_canonical_bytes,
        )
        return _decode_mapping_bytes(
            self._semantics_canonical_bytes, "r3 disposition semantics"
        )

    @property
    def semantics_sha256(self) -> str:
        """Return the digest of this record's validated sealed semantics."""
        _validate_record_content(
            self.game_pk,
            self.projection_index,
            self._query_canonical_bytes,
            self._semantics_canonical_bytes,
        )
        return hashlib.sha256(self._semantics_canonical_bytes).hexdigest()

    @property
    def endpoint(self) -> str:
        try:
            return _RECORD_ENDPOINTS[
                _RECORD_PAIRS.index((self.game_pk, self.projection_index))
            ]
        except ValueError as exc:
            raise ValueError("r3 disposition record identity differs") from exc

    @property
    def action(self) -> str | None:
        return "EXCLUDE" if self.game_pk == 632924 else None

    @property
    def reason(self) -> str | None:
        return "MISSING_VENUE" if self.game_pk == 632924 else None


@dataclass(frozen=True)
class MissingVenueRecoveryDispositionCatalog:
    records: tuple[MissingVenueDispositionRecord, ...]
    authority_run_spec_sha256: str
    authority_acquisition_contract_sha256: str

    def __post_init__(self) -> None:
        if (
            self.authority_run_spec_sha256 != _R3_RUN_SHA256
            or self.authority_acquisition_contract_sha256 != _R3_CONTRACT_SHA256
            or not isinstance(self.records, tuple)
            or tuple(
                (record.game_pk, record.projection_index) for record in self.records
            )
            != _RECORD_PAIRS
        ):
            raise ValueError("r3 disposition catalog identity differs")
        for record in self.records:
            if not isinstance(record, MissingVenueDispositionRecord):
                raise ValueError("r3 disposition catalog records differ")
            _validate_record_content(
                record.game_pk,
                record.projection_index,
                record._query_canonical_bytes,
                record._semantics_canonical_bytes,
            )
        if self.projection_query_ids() != tuple(
            logical_query_id(record.query) for record in self.records[:3]
        ) or self.excluded_game_pks() != frozenset({632924}):
            raise ValueError("r3 disposition catalog projections differ")

    def game_pks(self) -> tuple[int, ...]:
        return tuple(record.game_pk for record in self.records)

    def record_to_projection_indexes(self) -> tuple[int, ...]:
        return tuple(record.projection_index for record in self.records)

    def projection_query_ids(self) -> tuple[str, ...]:
        values: list[str] = []
        for record in self.records:
            value = logical_query_id(record.query)
            if value not in values:
                values.append(value)
        return tuple(values)

    def projection_query_for_game_pk(self, game_pk: int) -> LogicalQuery:
        return self.for_game_pk(game_pk).query

    def excluded_game_pks(self) -> frozenset[int]:
        return frozenset(
            record.game_pk for record in self.records if record.action == "EXCLUDE"
        )

    def as_projection(self) -> RecoveryDispositionProjection:
        return RecoveryDispositionProjection(
            record_game_pks=self.game_pks(),
            projection_query_ids=self.projection_query_ids(),
            record_to_projection_indexes=self.record_to_projection_indexes(),
            excluded_game_pks=self.excluded_game_pks(),
        )

    def for_game_pk(self, game_pk: int) -> MissingVenueDispositionRecord:
        matches = tuple(record for record in self.records if record.game_pk == game_pk)
        if len(matches) != 1:
            raise ValueError("r3 disposition game_pk is not cataloged")
        return matches[0]

    def records_for_query(
        self, query: LogicalQuery
    ) -> tuple[MissingVenueDispositionRecord, ...]:
        matches = tuple(record for record in self.records if record.query == query)
        if not matches:
            raise ValueError("r3 disposition query is not cataloged")
        return matches


def _protected_parent_root(repository_root: Path) -> Path:
    configured = os.environ.get(
        "MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT"
    ) or os.environ.get("MLB_V52_SOURCE_AUTHORITY_ROOT")
    candidate = Path(configured).resolve(strict=True) if configured else repository_root
    if not candidate.is_dir():
        raise ValueError("r3 protected parent evidence root differs")
    return candidate


def _validate_recovery_parents(
    parent_catalog: RecoveryParentCatalog, projection_queries: tuple[LogicalQuery, ...]
) -> None:
    if parent_catalog.catalog_kind != "recovery" or tuple(parent_catalog.parents) != (
        "original_2019_565534",
        "superseding_2020_630882",
        "superseding_2021_633224",
    ):
        raise ValueError("r3 recovery parent catalog differs")
    if tuple(query.endpoint for query in projection_queries) != _PROJECTION_ENDPOINTS:
        raise ValueError("r3 recovery projection queries differ")
    for authority in _R3_CONFLICT_AUTHORITY:
        _catalog_parent_binding(parent_catalog, authority)
    parent_catalog.reauthenticate()


def _require_occurrence(
    game: Mapping[str, object],
    expected: Mapping[str, object],
    fixed: Mapping[str, object],
) -> None:
    for key, source_key in (
        ("game_date", "gameDate"),
        ("venue_id", None),
        ("calendar_event_id", "calendarEventID"),
        ("resume_date", "resumeDate"),
        ("resume_game_date", "resumeGameDate"),
        ("resumed_from", "resumedFrom"),
        ("resumed_from_date", "resumedFromDate"),
        ("terminal_status", None),
    ):
        if key not in expected:
            continue
        actual = (
            game.get(source_key)
            if source_key
            else (
                game.get("venue", {}).get("id")
                if key == "venue_id" and isinstance(game.get("venue"), Mapping)
                else game.get("status", {}).get("detailedState")
                if key == "terminal_status" and isinstance(game.get("status"), Mapping)
                else None
            )
        )
        if actual != expected[key]:
            raise ValueError("r3 conflict occurrence field differs")
    if hashlib.sha256(canonical_json_bytes(game)).hexdigest() != expected["sha256"]:
        raise ValueError("r3 conflict raw object digest differs")
    teams = game.get("teams")
    if (
        not isinstance(teams, Mapping)
        or not isinstance(teams.get("away"), Mapping)
        or not isinstance(teams.get("home"), Mapping)
    ):
        raise ValueError("r3 conflict teams differ")
    away, home = teams["away"], teams["home"]
    away_team, home_team, status = (
        away.get("team"),
        home.get("team"),
        game.get("status"),
    )
    if (
        not isinstance(away_team, Mapping)
        or not isinstance(home_team, Mapping)
        or not isinstance(status, Mapping)
    ):
        raise ValueError("r3 conflict team/status differs")
    if game.get("gameType") != "R" or game.get("gamePk") != fixed["game_pk"]:
        raise ValueError("r3 conflict game identity differs")
    if (
        game.get("season") != str(fixed["season"])
        or game.get("officialDate") != fixed["official_date"]
        or away_team.get("id") != fixed["away_team_id"]
        or home_team.get("id") != fixed["home_team_id"]
    ):
        raise ValueError("r3 conflict fixed game facts differ")
    expected_status = expected.get("terminal_status", fixed["terminal_status"])
    if status.get("detailedState") != expected_status:
        raise ValueError("r3 conflict source status differs")
    if expected_status == fixed["terminal_status"] and (
        away.get("score") != fixed["away_score"]
        or home.get("score") != fixed["home_score"]
        or away.get("isWinner")
        is not (fixed["winner_team_id"] == fixed["away_team_id"])
        or home.get("isWinner")
        is not (fixed["winner_team_id"] == fixed["home_team_id"])
    ):
        raise ValueError("r3 conflict source outcome differs")


def _selected_occurrences(
    occurrences: list[tuple[int, int, Mapping[str, object], Mapping[str, object]]],
) -> list[
    tuple[
        dict[str, object], tuple[int, int, Mapping[str, object], Mapping[str, object]]
    ]
]:
    selected: dict[
        int,
        tuple[
            dict[str, object],
            tuple[int, int, Mapping[str, object], Mapping[str, object]],
        ],
    ] = {}
    order: list[int] = []
    for index, occurrence in enumerate(occurrences):
        _, _, date_entry, raw_game = occurrence
        game = _normalize_game(raw_game, dict(date_entry), index)
        if game is None:
            continue
        game_pk = game["game_pk"]
        prior = selected.get(game_pk)
        if prior is None:
            selected[game_pk] = (game, occurrence)
            order.append(game_pk)
        elif prior[0] != game:
            comparable = (set(prior[0]) | set(game)) - {"scheduled_start_utc"}
            final = {"Final", "Completed Early"}
            if prior[0]["status"] in final and game["status"] in final:
                if any(prior[0].get(key) != game.get(key) for key in comparable):
                    raise ValueError("r3 ordinary duplicate differs")
                if game["scheduled_start_utc"] < prior[0]["scheduled_start_utc"]:
                    selected[game_pk] = (game, occurrence)
            elif game["status"] in final or prior[0]["status"] not in final:
                selected[game_pk] = (game, occurrence)
    return [selected[game_pk] for game_pk in order]


def _reconcile_authenticated_raw(
    authority: _R3ConflictAuthority, raw: bytes
) -> tuple[Mapping[str, object], str]:
    """Pure r3 reconciliation: authenticated bytes and sealed positions only, no I/O."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("r3 parent raw is not JSON") from exc
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("totalGames")) is not int
        or not isinstance(payload.get("dates"), list)
    ):
        raise ValueError("r3 parent schedule root differs")
    occurrences: list[tuple[int, int, Mapping[str, object], Mapping[str, object]]] = []
    for date_index, date_entry in enumerate(payload["dates"]):
        if (
            not isinstance(date_entry, Mapping)
            or type(date_entry.get("totalGames")) is not int
            or not isinstance(date_entry.get("games"), list)
            or date_entry["totalGames"] != len(date_entry["games"])
        ):
            raise ValueError("r3 parent schedule date differs")
        for game_index, game in enumerate(date_entry["games"]):
            if not isinstance(game, Mapping):
                raise ValueError("r3 parent schedule game differs")
            if game.get("gameType") == "R":
                occurrences.append((date_index, game_index, date_entry, game))
    positions = authority.suppressed_positions + (authority.retained_position,)
    special = {
        (date_index, game_index): game
        for date_index, game_index, _, game in occurrences
        if (date_index, game_index) in positions
    }
    if set(special) != set(positions):
        raise ValueError("r3 conflict object positions differ")
    fixed = dict(authority.fixed_game_facts)
    fixed["game_pk"] = authority.game_pk
    for position, expected in zip(
        authority.suppressed_positions, authority.suppressed_occurrences, strict=True
    ):
        _require_occurrence(special[position], expected, fixed)
    _require_occurrence(
        special[authority.retained_position], authority.retained_occurrence, fixed
    )
    game_pks = [game.get("gamePk") for _, _, _, game in occurrences]
    if not all(type(game_pk) is int for game_pk in game_pks):
        raise ValueError("r3 regular gamePk differs")
    multiplicities = Counter(game_pks)
    counts = {
        "raw_total_games": payload["totalGames"],
        "raw_date_total_games": sum(
            int(item["totalGames"]) for item in payload["dates"]
        ),
        "raw_regular_occurrences": len(occurrences),
        "raw_unique_game_pks": len(multiplicities),
        "raw_duplicate_game_pks": sum(count > 1 for count in multiplicities.values()),
        "special_suppressed_occurrences": len(authority.suppressed_positions),
        "projected_occurrences": len(occurrences) - len(authority.suppressed_positions),
    }
    if "raw_duplicate_member_occurrences" in authority.counts:
        counts["raw_duplicate_member_occurrences"] = sum(
            count for count in multiplicities.values() if count > 1
        )
        counts["raw_duplicate_extra_occurrences"] = len(occurrences) - len(
            multiplicities
        )
    else:
        counts["ordinary_reconciled_duplicate_game_pks"] = (
            counts["raw_duplicate_game_pks"] - 1
        )
    selected = _selected_occurrences(
        [
            entry
            for entry in occurrences
            if entry[:2] not in authority.suppressed_positions
        ]
    )
    counts["normalized_unique_games"] = len(selected)
    if counts != dict(authority.counts):
        raise ValueError("r3 conflict occurrence counts differ")
    filtered = copy.deepcopy(payload)
    for date_index, game_index in sorted(authority.suppressed_positions, reverse=True):
        filtered["dates"][date_index]["games"].pop(game_index)
        filtered["dates"][date_index]["totalGames"] -= 1
        filtered["totalGames"] -= 1
    normalized = parse_schedule(canonical_json_bytes(filtered))
    if (
        normalized != [game for game, _ in selected]
        or canonical_sha256(normalized) != authority.normalized_games_sha256
    ):
        raise ValueError("r3 conflict normalized population differs")
    selected_rows = sorted(
        (
            {
                "game_pk": game["game_pk"],
                "raw_game_object_sha256": hashlib.sha256(
                    canonical_json_bytes(raw_game)
                ).hexdigest(),
            }
            for game, (_, _, _, raw_game) in selected
        ),
        key=lambda row: row["game_pk"],
    )
    if (
        canonical_sha256(selected_rows)
        != authority.selected_raw_game_object_sha256s_sha256
    ):
        raise ValueError("r3 conflict selected raw-object digest differs")
    return _frozen_mapping(
        {
            "game_pk": authority.game_pk,
            "fixed_game_facts": authority.fixed_game_facts,
            "retained_occurrence": authority.retained_occurrence,
            "occurrence_counts": authority.counts,
            "normalized_games_sha256": authority.normalized_games_sha256,
            "selected_raw_game_object_sha256s_sha256": authority.selected_raw_game_object_sha256s_sha256,
            **(
                {"suppressed_occurrence": authority.suppressed_occurrences[0]}
                if len(authority.suppressed_occurrences) == 1
                else {"suppressed_occurrences": authority.suppressed_occurrences}
            ),
            **(
                {
                    "played_date": authority.played_date,
                    "result_available_on": authority.result_available_on,
                }
                if authority.played_date is not None
                else {}
            ),
        },
        "r3 recomputed conflict semantics",
    ), special[authority.retained_position]["gameDate"]


def _build_r3_conflict_records(
    parent_catalog: RecoveryParentCatalog,
    projection_queries: tuple[LogicalQuery, ...],
    authenticated_raw_by_parent: Mapping[str, bytes] | None = None,
) -> tuple[MissingVenueDispositionRecord, ...]:
    """Build from sealed r3 authority and already-authenticated parent bytes."""
    if tuple(query.endpoint for query in projection_queries) != _PROJECTION_ENDPOINTS:
        raise ValueError("r3 conflict projection query identities differ")
    records: list[MissingVenueDispositionRecord] = []
    raw_by_parent = authenticated_raw_by_parent or {
        authority.parent_name: parent_catalog.read_raw(authority.parent_name)
        for authority in _R3_CONFLICT_AUTHORITY
    }
    for projection_index, authority in enumerate(_R3_CONFLICT_AUTHORITY):
        query = projection_queries[projection_index]
        parent_binding = _catalog_parent_binding(parent_catalog, authority)
        try:
            reconciled, retained_start = _reconcile_authenticated_raw(
                authority, raw_by_parent[authority.parent_name]
            )
        except KeyError as exc:
            raise ValueError("r3 authenticated parent bytes differ") from exc
        semantics = dict(_plain(reconciled))
        semantics.update(
            {
                "projection_query_endpoint": query.endpoint,
                _PARENT_RECONCILED_KEY: True,
                "parent_evidence": parent_binding,
                "retained_scheduled_start_utc": retained_start,
            }
        )
        semantics = _frozen_mapping(semantics, "r3 recomputed conflict semantics")
        if semantics != _expected_conflict_semantics(authority, query, parent_binding):
            raise ValueError("r3 conflict recomputation semantics differs")
        records.append(
            MissingVenueDispositionRecord(
                authority.game_pk, projection_index, query, semantics
            )
        )
    if (
        tuple((record.game_pk, record.projection_index) for record in records)
        != _RECORD_PAIRS[:3]
    ):
        raise ValueError("r3 conflict record order differs")
    return tuple(records)


def build_missing_venue_recovery_disposition_catalog(
    root: Path, run_spec_path: Path, acquisition_contract_path: Path
) -> MissingVenueRecoveryDispositionCatalog:
    """Build only from tracked r3 authority, its full batch, and protected bytes."""
    repository_root = Path(root).resolve(strict=True)
    context = _load_static_contracts(run_spec_path, acquisition_contract_path)
    if (
        context.repository_root != repository_root
        or context.run_id != R3_RECOVERY_RUN_ID
        or context.authority_path != repository_root / R3_RUN_SPEC_RELATIVE
        or context.acquisition_contract_path
        != repository_root / R3_ACQUISITION_CONTRACT_RELATIVE
        or context.authority_sha256 != _R3_RUN_SHA256
        or context.acquisition_contract_sha256 != _R3_CONTRACT_SHA256
    ):
        raise ValueError("r3 disposition authority differs")
    static = build_static_batch(
        context.authority_path, context.acquisition_contract_path
    )
    queries = _validate_r3_static_batch(
        static, context.authority_sha256, context.acquisition_contract_sha256
    )
    projections = tuple(
        query for query in queries if query.endpoint.startswith("disposition://")
    )
    parent_root = _protected_parent_root(repository_root)
    recovery_parents = build_recovery_parent_catalog(parent_root)
    _validate_recovery_parents(recovery_parents, projections)
    raw_by_parent = {
        authority.parent_name: recovery_parents.read_raw(authority.parent_name)
        for authority in _R3_CONFLICT_AUTHORITY
    }
    parent_binding = _missing_venue_parent_binding(recovery_parents)
    raw_object = _missing_venue_raw_object(raw_by_parent["superseding_2021_633224"])
    conflict_records = _build_r3_conflict_records(
        recovery_parents, projections, raw_by_parent
    )
    recovery_parents.reauthenticate()
    records = (
        *conflict_records,
        MissingVenueDispositionRecord(
            632924,
            2,
            projections[2],
            {
                "game_pk": 632924,
                "excluded_game_pks": [632924],
                "raw_object_sha256": _MISSING_VENUE_OBJECT_SHA256,
                "raw_object": raw_object,
                "parent_binding": parent_binding,
                "shared_2021_conflict_semantics": conflict_records[2].semantics,
            },
        ),
    )
    return MissingVenueRecoveryDispositionCatalog(
        records, context.authority_sha256, context.acquisition_contract_sha256
    )


__all__ = [
    "MissingVenueDispositionRecord",
    "MissingVenueRecoveryDispositionCatalog",
    "R3_ACQUISITION_CONTRACT_RELATIVE",
    "R3_RECOVERY_RUN_ID",
    "R3_RUN_SPEC_RELATIVE",
    "build_missing_venue_recovery_disposition_catalog",
]
