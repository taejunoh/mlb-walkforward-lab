"""Closed, parent-authenticated dispositions for the recovery schedule run."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import deep_freeze
from .source_parsers.schedule import _normalize_game, parse_schedule
from .v52_evidence import LogicalQuery, logical_query_id
from .v52_query_plan import _load_static_contracts, build_static_batch
from .v52_recovery_parent_evidence import (
    RecoveryParentCatalog,
    build_recovery_parent_catalog,
)
from .v52_source_contracts import canonical_json_bytes, canonical_sha256


RECOVERY_DISPOSITION_ENDPOINTS = (
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
)

_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts"


@dataclass(frozen=True)
class RecoveryDispositionSemantics:
    game_pk: int
    query: LogicalQuery
    expected_sidecar_semantics: Mapping[str, object]
    sidecar_semantics_sha256: str
    expected_derived_sidecar: Mapping[str, object]
    expected_derived_sidecar_sha256: str
    retained_scheduled_start_utc: str
    played_date: str | None
    result_available_on: str | None
    raw_object_sha256s: tuple[str, ...]
    selected_raw_game_object_sha256s_sha256: str
    normalized_games_sha256: str


@dataclass(frozen=True)
class RecoveryDispositionAuthority:
    repository_root: Path
    source_run_spec_path: Path
    acquisition_contract_path: Path
    parent_catalog: RecoveryParentCatalog
    evidence_root: Path
    query: LogicalQuery
    semantics: RecoveryDispositionSemantics

    def __post_init__(self) -> None:
        if self.query != self.semantics.query:
            raise ValueError("recovery disposition authority query differs from semantics")

    @property
    def game_pk(self) -> int:
        return self.semantics.game_pk

    def sidecar_semantics(self) -> Mapping[str, object]:
        rebound = self.reauthenticate()
        return _plain(rebound.semantics.expected_sidecar_semantics)  # type: ignore[return-value]

    def derived_sidecar(self) -> Mapping[str, object]:
        rebound = self.reauthenticate()
        return _plain(rebound.semantics.expected_derived_sidecar)  # type: ignore[return-value]

    def reauthenticate(self) -> RecoveryDispositionAuthority:
        rebound = build_recovery_schedule_conflict_catalog(
            self.repository_root,
            self.source_run_spec_path,
            self.acquisition_contract_path,
        ).for_query(self.query)
        if rebound != self:
            raise ValueError(
                "recovery disposition authority changed during reauthentication"
            )
        return rebound


@dataclass(frozen=True)
class RecoveryDispositionCatalog:
    entries: tuple[RecoveryDispositionAuthority, ...]

    def __post_init__(self) -> None:
        if len(self.entries) != 3:
            raise ValueError(
                "recovery disposition catalog must contain exactly three entries"
            )
        if tuple(entry.game_pk for entry in self.entries) != (565534, 630882, 633224):
            raise ValueError("recovery disposition catalog game_pk order differs")

    def for_game_pk(self, game_pk: int) -> RecoveryDispositionAuthority:
        matches = tuple(entry for entry in self.entries if entry.game_pk == game_pk)
        if not matches:
            raise ValueError("recovery disposition game_pk is not cataloged")
        if len(matches) != 1:
            raise ValueError("recovery disposition catalog has duplicate game_pk")
        return matches[0]

    def for_query(self, query: LogicalQuery) -> RecoveryDispositionAuthority:
        matches = tuple(entry for entry in self.entries if entry.query == query)
        if not matches:
            raise ValueError("recovery disposition query is not cataloged")
        if len(matches) != 1:
            raise ValueError("recovery disposition catalog has duplicate query")
        return matches[0]

    def query_ids(self) -> tuple[str, ...]:
        return tuple(logical_query_id(entry.query) for entry in self.entries)


@dataclass(frozen=True)
class RecoveryDispositionBinding:
    game_pk: int
    query_id: str
    query_sidecar: Mapping[str, str]
    success_receipt: Mapping[str, str]
    retained_scheduled_start_utc: str
    played_date: str | None
    result_available_on: str | None
    raw_object_sha256s: tuple[str, ...]
    selected_raw_game_object_sha256s_sha256: str
    normalized_games_sha256: str
    sidecar_semantics_sha256: str
    expected_derived_sidecar_sha256: str

    def __post_init__(self) -> None:
        for name in ("query_sidecar", "success_receipt"):
            value = getattr(self, name)
            if set(value) != {"path", "sha256"} or not all(
                isinstance(item, str) for item in value.values()
            ):
                raise ValueError(f"recovery disposition {name} binding differs")
            object.__setattr__(self, name, deep_freeze(dict(value)))


def load_recovery_disposition_bindings(
    catalog: RecoveryDispositionCatalog,
) -> tuple[RecoveryDispositionBinding, ...]:
    if not isinstance(catalog, RecoveryDispositionCatalog):
        raise TypeError("recovery disposition catalog is required")
    authorities = tuple(entry.reauthenticate() for entry in catalog.entries)
    if authorities != catalog.entries:
        raise ValueError("recovery disposition catalog changed before binding load")
    roots = {entry.evidence_root for entry in authorities}
    if len(roots) != 1:
        raise ValueError("recovery disposition evidence roots differ")
    from .v52_evidence import V52EvidenceStore

    try:
        store = V52EvidenceStore.open_read_only(next(iter(roots)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("recovery disposition success evidence is unavailable") from exc

    def read(authority: RecoveryDispositionAuthority) -> RecoveryDispositionBinding:
        try:
            verified = store.read_verified_success(
                authority.query, authority=authority
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "recovery disposition success evidence is unavailable"
            ) from exc
        published = verified.published_evidence
        sidecar = verified.query_sidecar
        success = verified.success_receipt
        expected_sidecar = _plain(authority.semantics.expected_derived_sidecar)
        if (
            published.storage_mode != "DERIVED_DISPOSITION"
            or published.raw_sha256 is not None
            or verified.raw_bytes is not None
            or verified.attempt_receipts
            or sidecar != expected_sidecar
            or _sha256_canonical_file(sidecar) != published.query_sidecar_sha256
            or _sha256_canonical(expected_sidecar)
            != authority.semantics.expected_derived_sidecar_sha256
        ):
            raise ValueError("recovery disposition sidecar binding differs")
        query_id = logical_query_id(authority.query)
        sidecar_path = (
            f"queries/{authority.query.source}/{query_id}/sidecars/"
            f"{published.query_sidecar_sha256}.json"
        )
        success_path = f"queries/{authority.query.source}/{query_id}/success.json"
        if (
            success.get("storage_mode") != "DERIVED_DISPOSITION"
            or success.get("query_id") != query_id
            or success.get("query_sidecar_path") != sidecar_path
            or success.get("query_sidecar_sha256") != published.query_sidecar_sha256
        ):
            raise ValueError("recovery disposition success binding differs")
        return RecoveryDispositionBinding(
            game_pk=authority.game_pk,
            query_id=query_id,
            query_sidecar={"path": sidecar_path, "sha256": published.query_sidecar_sha256},
            success_receipt={
                "path": success_path,
                "sha256": _sha256_canonical_file(success),
            },
            retained_scheduled_start_utc=authority.semantics.retained_scheduled_start_utc,
            played_date=authority.semantics.played_date,
            result_available_on=authority.semantics.result_available_on,
            raw_object_sha256s=authority.semantics.raw_object_sha256s,
            selected_raw_game_object_sha256s_sha256=(
                authority.semantics.selected_raw_game_object_sha256s_sha256
            ),
            normalized_games_sha256=authority.semantics.normalized_games_sha256,
            sidecar_semantics_sha256=authority.semantics.sidecar_semantics_sha256,
            expected_derived_sidecar_sha256=(
                authority.semantics.expected_derived_sidecar_sha256
            ),
        )

    bindings = tuple(read(authority) for authority in authorities)
    if tuple(binding.game_pk for binding in bindings) != (565534, 630882, 633224):
        raise ValueError("recovery disposition binding order differs")
    final_authorities = tuple(entry.reauthenticate() for entry in catalog.entries)
    if final_authorities != authorities:
        raise ValueError("recovery disposition catalog changed during binding load")
    final = tuple(read(authority) for authority in final_authorities)
    if final != bindings:
        raise ValueError("recovery disposition bindings changed during final replay")
    return bindings


@dataclass(frozen=True)
class _DispositionSpec:
    game_pk: int
    parent_name: str
    endpoint: str
    suppressed_positions: tuple[tuple[int, int], ...]
    retained_position: tuple[int, int]
    raw_object_sha256s: tuple[str, ...]
    normalized_games_sha256: str
    selected_raw_game_object_sha256s_sha256: str
    counts: Mapping[str, int]
    fixed_game_facts: Mapping[str, object]
    suppressed_occurrences: tuple[Mapping[str, object], ...]
    retained_occurrence: Mapping[str, object]
    played_date: str | None = None
    result_available_on: str | None = None


def _frozen(value: object) -> Mapping[str, object]:
    frozen = deep_freeze(_plain(value))
    if not isinstance(frozen, Mapping):
        raise AssertionError("closed disposition semantics must be a mapping")
    return frozen


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()


def _sha256_canonical_file(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(_plain(value)) + b"\n").hexdigest()


def _freeze_logical_query(query: LogicalQuery) -> LogicalQuery:
    """Copy the Task 2 identity into JSON-compatible immutable mappings."""
    if not isinstance(query, LogicalQuery):
        raise TypeError("recovery disposition query must be a LogicalQuery")
    parameters = _frozen(query.parameters)
    requested_range = _frozen(query.requested_range)
    return LogicalQuery(
        source=query.source,
        grain=query.grain,
        endpoint=query.endpoint,
        parameters=parameters,
        requested_range=requested_range,
        expected_media_type=query.expected_media_type,
        parser_version=query.parser_version,
        parent_query_id=query.parent_query_id,
        run_spec_sha256=query.run_spec_sha256,
        acquisition_contract_sha256=query.acquisition_contract_sha256,
    )


_SPECS = (
    _DispositionSpec(
        565534,
        "original_2019_565534",
        RECOVERY_DISPOSITION_ENDPOINTS[0],
        ((54, 5),),
        (161, 12),
        (
            "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941",
            "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6",
        ),
        "a6089e74246b919b59519b93743a0281834ef759347faa67f457d5deb5191930",
        "114d3593ebee215b357622ee712011314061d0ded3e888d847dc90270a25a360",
        _frozen(
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
        ),
        _frozen(
            {
                "season": 2019,
                "official_date": "2019-05-19",
                "away_team_id": 133,
                "home_team_id": 116,
                "terminal_status": "Final",
                "away_score": 7,
                "home_score": 3,
                "winner_team_id": 133,
            }
        ),
        (
            _frozen(
                {
                    "date_index": 54,
                    "game_index": 5,
                    "sha256": "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941",
                    "game_date": "2019-05-19T17:10:00Z",
                    "venue_id": 2394,
                    "calendar_event_id": "14-565534-2019-05-19",
                    "resume_date": "2019-09-07T00:15:00Z",
                    "resume_game_date": "2019-09-06",
                }
            ),
        ),
        _frozen(
            {
                "date_index": 161,
                "game_index": 12,
                "sha256": "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6",
                "game_date": "2019-09-07T00:15:00Z",
                "venue_id": 10,
                "calendar_event_id": "14-565534-2019-09-06",
                "resumed_from": "2019-05-19T17:10:00Z",
                "resumed_from_date": "2019-05-19",
            }
        ),
    ),
    _DispositionSpec(
        630882,
        "superseding_2020_630882",
        RECOVERY_DISPOSITION_ENDPOINTS[1],
        ((17, 0),),
        (22, 0),
        (
            "2a5b251745546e643897cfc01eaa4c63c949bb8d8243b7fec44d3a23484f9be4",
            "0d961f12abb78e3b833c9f5c5bbbff76cad80a5370f57786904c7b42e82b204e",
        ),
        "536d5b7e52e8ce3445abd31c3bbac4ba1ec82b44c987accd8c1d428ef139f8d9",
        "54c39b5be026844e5b8bb1fe2058fece1eee75f634d2c8c2f2f96cf331f494b1",
        _frozen(
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
            }
        ),
        _frozen(
            {
                "season": 2020,
                "official_date": "2020-08-09",
                "away_team_id": 110,
                "home_team_id": 120,
                "terminal_status": "Final",
                "away_score": 6,
                "home_score": 2,
                "winner_team_id": 110,
            }
        ),
        (
            _frozen(
                {
                    "date_index": 17,
                    "game_index": 0,
                    "sha256": "2a5b251745546e643897cfc01eaa4c63c949bb8d8243b7fec44d3a23484f9be4",
                    "game_date": "2020-08-09T16:35:00Z",
                    "venue_id": 3309,
                    "resume_date": "2020-08-14T21:05:00Z",
                    "resume_game_date": "2020-08-14",
                }
            ),
        ),
        _frozen(
            {
                "date_index": 22,
                "game_index": 0,
                "sha256": "0d961f12abb78e3b833c9f5c5bbbff76cad80a5370f57786904c7b42e82b204e",
                "game_date": "2020-08-14T21:05:00Z",
                "venue_id": 2,
                "resumed_from": "2020-08-09T16:35:00Z",
                "resumed_from_date": "2020-08-09",
            }
        ),
        "2020-08-14",
        "2020-08-14",
    ),
    _DispositionSpec(
        633224,
        "superseding_2021_633224",
        RECOVERY_DISPOSITION_ENDPOINTS[2],
        ((106, 12), (108, 2)),
        (173, 11),
        (
            "b5d52997a362f89e669e1454960b5230d4977f30370ad3f3ad7ca1b12be5221d",
            "25f8ab0769219c592779cd800bb2b3278c9c75644647bd51ae5db3051bcf5a61",
            "109a5d145731938e08f516f19345557894316b3a72687f4ac63029df07afe94c",
        ),
        "891f73f68c3108ccf78f11f0ee11125bd6fdb4dc71e17bddadbcb8d88c008769",
        "17362576e0ae9e96a0e20979a9f8afdfbb95de03e2aa3fd4d66a4aa63486681f",
        _frozen(
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
            }
        ),
        _frozen(
            {
                "season": 2021,
                "official_date": "2021-07-21",
                "away_team_id": 135,
                "home_team_id": 144,
                "terminal_status": "Final",
                "away_score": 6,
                "home_score": 5,
                "winner_team_id": 135,
            }
        ),
        (
            _frozen(
                {
                    "date_index": 106,
                    "game_index": 12,
                    "sha256": "b5d52997a362f89e669e1454960b5230d4977f30370ad3f3ad7ca1b12be5221d",
                    "game_date": "2021-07-19T23:20:00Z",
                    "venue_id": 4705,
                    "terminal_status": "Postponed",
                }
            ),
            _frozen(
                {
                    "date_index": 108,
                    "game_index": 2,
                    "sha256": "25f8ab0769219c592779cd800bb2b3278c9c75644647bd51ae5db3051bcf5a61",
                    "game_date": "2021-07-21T21:20:00Z",
                    "venue_id": 4705,
                    "terminal_status": "Final",
                    "resume_date": "2021-09-25T00:10:00Z",
                    "resume_game_date": "2021-09-24",
                }
            ),
        ),
        _frozen(
            {
                "date_index": 173,
                "game_index": 11,
                "sha256": "109a5d145731938e08f516f19345557894316b3a72687f4ac63029df07afe94c",
                "game_date": "2021-09-25T00:10:00Z",
                "venue_id": 2680,
                "terminal_status": "Final",
                "resumed_from": "2021-07-21T21:20:00Z",
                "resumed_from_date": "2021-07-21",
            }
        ),
        "2021-09-24",
        "2021-09-24",
    ),
)


def _parent_authority(
    catalog: RecoveryParentCatalog, parent_name: str
) -> Mapping[str, object]:
    parent = catalog.parents[parent_name]
    values: dict[str, object] = {
        "query_id": parent.query_id,
        "query_registration": {
            "path": parent.registration.relative.as_posix(),
            "sha256": parent.registration.sha256,
        },
        "quarantine_receipt": {
            "path": parent.quarantine.relative.as_posix(),
            "sha256": parent.quarantine.sha256,
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
        }
    return _frozen(values)


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
            raise ValueError("recovery disposition occurrence field differs")
    if hashlib.sha256(canonical_json_bytes(game)).hexdigest() != expected["sha256"]:
        raise ValueError("recovery disposition raw object digest differs")
    teams = game.get("teams")
    if (
        not isinstance(teams, Mapping)
        or not isinstance(teams.get("away"), Mapping)
        or not isinstance(teams.get("home"), Mapping)
    ):
        raise ValueError("recovery disposition teams differ")
    away, home = teams["away"], teams["home"]
    away_team, home_team = away.get("team"), home.get("team")
    status = game.get("status")
    if (
        not isinstance(away_team, Mapping)
        or not isinstance(home_team, Mapping)
        or not isinstance(status, Mapping)
    ):
        raise ValueError("recovery disposition team/status differs")
    if game.get("gameType") != "R" or game.get("gamePk") != fixed.get(
        "game_pk", game.get("gamePk")
    ):
        raise ValueError("recovery disposition game identity differs")
    for semantic, source in (
        ("season", "season"),
        ("official_date", "officialDate"),
        ("away_team_id", None),
        ("home_team_id", None),
    ):
        value = (
            game.get(source)
            if source
            else (
                away_team.get("id")
                if semantic == "away_team_id"
                else home_team.get("id")
            )
        )
        expected_value = (
            str(fixed[semantic]) if semantic == "season" else fixed[semantic]
        )
        if value != expected_value:
            raise ValueError("recovery disposition fixed game facts differ")
    expected_status = expected.get("terminal_status", fixed["terminal_status"])
    if status.get("detailedState") != expected_status:
        raise ValueError("recovery disposition source status differs")
    if expected_status == fixed["terminal_status"]:
        if (
            away.get("score") != fixed["away_score"]
            or home.get("score") != fixed["home_score"]
            or away.get("isWinner") is not (fixed["winner_team_id"] == fixed["away_team_id"])
            or home.get("isWinner") is not (fixed["winner_team_id"] == fixed["home_team_id"])
        ):
            raise ValueError("recovery disposition source outcome differs")


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
            prior_game = prior[0]
            comparable = (set(prior_game) | set(game)) - {"scheduled_start_utc"}
            final = {"Final", "Completed Early"}
            if prior_game["status"] in final and game["status"] in final:
                if any(prior_game.get(key) != game.get(key) for key in comparable):
                    raise ValueError("recovery disposition ordinary duplicate differs")
                if game["scheduled_start_utc"] < prior_game["scheduled_start_utc"]:
                    selected[game_pk] = (game, occurrence)
            elif game["status"] in final or prior_game["status"] not in final:
                selected[game_pk] = (game, occurrence)
    return [selected[game_pk] for game_pk in order]


def _recompute(
    spec: _DispositionSpec, parent_catalog: RecoveryParentCatalog
) -> tuple[Mapping[str, object], str, str]:
    raw = parent_catalog.read_raw(spec.parent_name)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery parent raw is not JSON") from exc
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("totalGames")) is not int
        or not isinstance(payload.get("dates"), list)
    ):
        raise ValueError("recovery parent schedule root differs")
    occurrences: list[tuple[int, int, Mapping[str, object], Mapping[str, object]]] = []
    for date_index, date_entry in enumerate(payload["dates"]):
        if (
            not isinstance(date_entry, Mapping)
            or type(date_entry.get("totalGames")) is not int
            or not isinstance(date_entry.get("games"), list)
            or date_entry["totalGames"] != len(date_entry["games"])
        ):
            raise ValueError("recovery parent schedule date differs")
        for game_index, game in enumerate(date_entry["games"]):
            if not isinstance(game, Mapping):
                raise ValueError("recovery parent schedule game differs")
            if game.get("gameType") == "R":
                occurrences.append((date_index, game_index, date_entry, game))
    positions = spec.suppressed_positions + (spec.retained_position,)
    special = {
        position: game
        for date_index, game_index, _, game in occurrences
        if (position := (date_index, game_index)) in positions
    }
    if set(special) != set(positions):
        raise ValueError("recovery disposition object positions differ")
    fixed = dict(spec.fixed_game_facts)
    fixed["game_pk"] = spec.game_pk
    for position, expected in zip(
        spec.suppressed_positions, spec.suppressed_occurrences, strict=True
    ):
        _require_occurrence(special[position], expected, fixed)
    _require_occurrence(
        special[spec.retained_position], spec.retained_occurrence, fixed
    )
    game_pks = [game.get("gamePk") for _, _, _, game in occurrences]
    if not all(type(game_pk) is int for game_pk in game_pks):
        raise ValueError("recovery regular gamePk differs")
    multiplicities = Counter(game_pks)
    counts = {
        "raw_total_games": payload["totalGames"],
        "raw_date_total_games": sum(
            int(item["totalGames"]) for item in payload["dates"]
        ),
        "raw_regular_occurrences": len(occurrences),
        "raw_unique_game_pks": len(multiplicities),
        "raw_duplicate_game_pks": sum(count > 1 for count in multiplicities.values()),
        "special_suppressed_occurrences": len(spec.suppressed_positions),
        "projected_occurrences": len(occurrences) - len(spec.suppressed_positions),
    }
    if "raw_duplicate_member_occurrences" in spec.counts:
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
    projected = [
        entry for entry in occurrences if entry[:2] not in spec.suppressed_positions
    ]
    selected = _selected_occurrences(projected)
    counts["normalized_unique_games"] = len(selected)
    if counts != dict(spec.counts):
        raise ValueError("recovery disposition occurrence counts differ")
    filtered = copy.deepcopy(payload)
    for date_index, game_index in sorted(spec.suppressed_positions, reverse=True):
        date_entry = filtered["dates"][date_index]
        del date_entry["games"][game_index]
        date_entry["totalGames"] -= 1
        filtered["totalGames"] -= 1
    normalized = parse_schedule(canonical_json_bytes(filtered))
    if normalized != [game for game, _ in selected]:
        raise ValueError("recovery disposition normalized population differs")
    normalized_sha = canonical_sha256(normalized)
    if normalized_sha != spec.normalized_games_sha256:
        raise ValueError("recovery disposition normalized digest differs")
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
    selected_sha = canonical_sha256(selected_rows)
    if selected_sha != spec.selected_raw_game_object_sha256s_sha256:
        raise ValueError("recovery disposition selected raw-object digest differs")
    retained = special[spec.retained_position]
    return (
        _semantics_mapping(spec, parent_catalog, counts),
        retained["gameDate"],
        normalized_sha,
    )


def _semantics_mapping(
    spec: _DispositionSpec, catalog: RecoveryParentCatalog, counts: Mapping[str, int]
) -> Mapping[str, object]:
    values: dict[str, object] = {
        "disposition": spec.endpoint.removeprefix("disposition://"),
        "game_pk": spec.game_pk,
        "old_authority": _parent_authority(catalog, spec.parent_name),
        "fixed_game_facts": dict(spec.fixed_game_facts),
        "retained_occurrence": dict(spec.retained_occurrence),
        "occurrence_counts": dict(counts),
        "normalized_games_sha256": spec.normalized_games_sha256,
        "selected_raw_game_object_sha256s_sha256": spec.selected_raw_game_object_sha256s_sha256,
    }
    if len(spec.suppressed_occurrences) == 1:
        values["suppressed_occurrence"] = dict(spec.suppressed_occurrences[0])
    else:
        values["suppressed_occurrences"] = [
            dict(item) for item in spec.suppressed_occurrences
        ]
    if spec.played_date is not None:
        values["played_date"] = spec.played_date
        values["result_available_on"] = spec.result_available_on
    return _frozen(values)


def build_recovery_schedule_conflict_catalog(
    root: Path, run_spec_path: Path, acquisition_contract_path: Path
) -> RecoveryDispositionCatalog:
    repository_root = Path(root).resolve(strict=True)
    context = _load_static_contracts(run_spec_path, acquisition_contract_path)
    if context.repository_root != repository_root or context.run_id != _RECOVERY_RUN_ID:
        raise ValueError(
            "recovery disposition catalog requires the authenticated recovery authority"
        )
    parent_catalog = build_recovery_parent_catalog(repository_root)
    static = build_static_batch(
        context.authority_path, context.acquisition_contract_path
    )
    queries = {
        query.endpoint: _freeze_logical_query(query)
        for query in static.queries
        if query.endpoint in RECOVERY_DISPOSITION_ENDPOINTS
    }
    if tuple(queries) != RECOVERY_DISPOSITION_ENDPOINTS:
        raise ValueError("recovery disposition query identities differ")
    entries: list[RecoveryDispositionAuthority] = []
    for spec in _SPECS:
        query = queries.get(spec.endpoint)
        if query is None:
            raise ValueError("recovery disposition query is missing")
        semantics_mapping, retained_start, normalized_sha = _recompute(
            spec, parent_catalog
        )
        derived = _frozen(
            {
                "schema_version": 1,
                "storage_mode": "DERIVED_DISPOSITION",
                "source": query.source,
                "grain": query.grain,
                "parser_version": query.parser_version,
                "query_id": logical_query_id(query),
                "semantics": semantics_mapping,
            }
        )
        semantics = RecoveryDispositionSemantics(
            spec.game_pk,
            query,
            semantics_mapping,
            canonical_sha256(_plain(semantics_mapping)),
            derived,
            canonical_sha256(_plain(derived)),
            retained_start,
            spec.played_date,
            spec.result_available_on,
            spec.raw_object_sha256s,
            spec.selected_raw_game_object_sha256s_sha256,
            normalized_sha,
        )
        entries.append(
            RecoveryDispositionAuthority(
                repository_root,
                context.authority_path,
                context.acquisition_contract_path,
                parent_catalog,
                context.raw_root,
                query,
                semantics,
            )
        )
    return RecoveryDispositionCatalog(tuple(entries))


__all__ = [
    "RECOVERY_DISPOSITION_ENDPOINTS",
    "RecoveryDispositionAuthority",
    "RecoveryDispositionBinding",
    "RecoveryDispositionCatalog",
    "RecoveryDispositionSemantics",
    "build_recovery_schedule_conflict_catalog",
    "load_recovery_disposition_bindings",
]
