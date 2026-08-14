"""MLB schedule anchors and joined final game evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..source_parsers.schedule import (
    parse_schedule,
    parse_schedule_occurrences,
    select_schedule_occurrences,
    validate_schedule_query_coverage,
)
from ..v52_evidence import LogicalQuery, logical_query_id
from ..v52_source_contracts import (
    QUERY_SIDECAR_KEYS,
    canonical_json_bytes,
    canonical_sha256,
    query_sidecar,
    require_query,
)


_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_PLAYED = frozenset({"Final", "Completed Early"})
_STARTING_BATTING_ORDERS = frozenset(
    str(slot) for slot in range(100, 1000, 100)
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_R3_MISSING_VENUE_POSITION = (130, 10)
_R3_POST_NORMALIZED_GAMES_SHA256 = (
    "8fc70974fcf58cacbd017c8a00e52cedc0f53c99bd6b035850d8e694e2f17263"
)
_R3_POST_SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256 = (
    "b66766a1395986a3a49e39708fe0de00702d93c1d13985a6018ac8f86d5fd148"
)
_SOURCE_SCHEDULE_ANCHOR_KEYS = frozenset(
    {
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
    }
)


@dataclass(frozen=True, slots=True)
class R3MissingVenueScheduleProjection:
    """One sealed 2021 r3 schedule view, after its sole exclusion."""

    raw: bytes
    pre_normalized_games_count: int
    pre_normalized_games_sha256: str
    pre_selected_raw_game_object_sha256s_sha256: str
    post_normalized_games_count: int
    post_normalized_games_sha256: str
    post_selected_raw_game_object_sha256s_sha256: str
    excluded_game_pks: frozenset[int]


def _r3_plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _r3_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_r3_plain(item) for item in value]
    return value


def _r3_population(raw: bytes) -> tuple[list[dict[str, object]], str, str]:
    """Return the normalized and selected-object digests for one filtered view."""
    normalized = parse_schedule(raw)
    selected_rows = sorted(
        (
            {
                "game_pk": game["game_pk"],
                "raw_game_object_sha256": hashlib.sha256(
                    canonical_json_bytes(raw_game)
                ).hexdigest(),
            }
            for game, (_date_index, _game_index, _date, raw_game) in
            select_schedule_occurrences(raw)
        ),
        key=lambda row: int(row["game_pk"]),
    )
    if len(normalized) != len(selected_rows) or {
        int(game["game_pk"]) for game in normalized
    } != {int(row["game_pk"]) for row in selected_rows}:
        raise ValueError("r3 schedule population differs")
    return (
        normalized,
        hashlib.sha256(canonical_json_bytes(normalized)).hexdigest(),
        hashlib.sha256(canonical_json_bytes(selected_rows)).hexdigest(),
    )


def _r3_delete_occurrences(
    payload: dict[str, object], positions: Sequence[tuple[int, int]]
) -> None:
    """Delete raw occurrences reverse-safely while preserving response totals."""
    dates = payload.get("dates")
    if (
        not isinstance(dates, list)
        or type(payload.get("totalGames")) is not int
        or payload["totalGames"] < 0
    ):
        raise ValueError("r3 schedule root differs")
    for date_index, game_index in sorted(positions, reverse=True):
        try:
            date_entry = dates[date_index]
            games = date_entry["games"]
            if (
                not isinstance(date_entry, dict)
                or not isinstance(games, list)
                or type(date_entry.get("totalGames")) is not int
                or date_entry["totalGames"] != len(games)
                or date_entry["totalGames"] <= 0
                or payload["totalGames"] <= 0
            ):
                raise ValueError("r3 schedule occurrence totals differ")
            games.pop(game_index)
            date_entry["totalGames"] -= 1
            payload["totalGames"] -= 1
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("r3 schedule occurrence position differs") from exc


def project_r3_missing_venue_schedule(
    raw: bytes, query: LogicalQuery, catalog: object
) -> R3MissingVenueScheduleProjection:
    """Apply the closed shared 2021 r3 projection before public parsing.

    The catalog projection capability is the sole authority for record/query
    topology and the excluded game set.  Record semantics authenticate the
    two 633224 suppressions and the exact missing-venue raw object.
    """
    from ..v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )

    if not isinstance(raw, bytes):
        raise TypeError("r3 schedule raw must be bytes")
    if not isinstance(query, LogicalQuery) or not isinstance(
        catalog, MissingVenueRecoveryDispositionCatalog
    ):
        raise ValueError("r3 schedule authority differs")
    projection = catalog.as_projection()
    query_id = logical_query_id(query)
    if (
        projection.record_game_pks != catalog.game_pks()
        or projection.projection_query_ids != catalog.projection_query_ids()
        or projection.record_to_projection_indexes != catalog.record_to_projection_indexes()
        or projection.excluded_game_pks != catalog.excluded_game_pks()
        or projection.excluded_game_pks != frozenset({632924})
    ):
        raise ValueError("r3 schedule projection topology differs")
    try:
        projection_index = projection.projection_query_ids.index(query_id)
    except ValueError as exc:
        raise ValueError("r3 schedule projection query differs") from exc
    record_game_pks = tuple(
        game_pk
        for game_pk, index in zip(
            projection.record_game_pks,
            projection.record_to_projection_indexes,
            strict=True,
        )
        if index == projection_index
    )
    records = catalog.records_for_query(query)
    if tuple(record.game_pk for record in records) != record_game_pks:
        raise ValueError("r3 schedule projection records differ")
    excluded_records = tuple(
        record for record in records if record.game_pk in projection.excluded_game_pks
    )
    conflict_records = tuple(record for record in records if record not in excluded_records)
    if len(excluded_records) != 1 or len(conflict_records) != 1:
        raise ValueError("r3 schedule projection records differ")
    excluded_record = excluded_records[0]
    conflict_record = conflict_records[0]
    conflict = conflict_record.semantics
    excluded = excluded_record.semantics
    if excluded.get("shared_2021_conflict_semantics") != conflict:
        raise ValueError("r3 shared 2021 semantics differ")
    suppressed = conflict.get("suppressed_occurrences")
    if not isinstance(suppressed, (list, tuple)) or not suppressed:
        raise ValueError("r3 conflict suppressions differ")
    positions: list[tuple[int, int]] = []
    for occurrence in suppressed:
        if not isinstance(occurrence, Mapping):
            raise ValueError("r3 conflict suppression differs")
        date_index = occurrence.get("date_index")
        game_index = occurrence.get("game_index")
        if (
            type(date_index) is not int
            or date_index < 0
            or type(game_index) is not int
            or game_index < 0
        ):
            raise ValueError("r3 conflict suppression differs")
        positions.append((date_index, game_index))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("r3 parent schedule differs") from exc
    if not isinstance(payload, dict):
        raise ValueError("r3 parent schedule differs")
    for position, occurrence in zip(positions, suppressed, strict=True):
        try:
            game = payload["dates"][position[0]]["games"][position[1]]
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("r3 conflict suppression differs") from exc
        if (
            not isinstance(game, Mapping)
            or hashlib.sha256(canonical_json_bytes(game)).hexdigest()
            != occurrence.get("sha256")
        ):
            raise ValueError("r3 conflict suppression differs")
    _r3_delete_occurrences(payload, positions)
    pre_raw = canonical_json_bytes(payload)
    pre_games, pre_normalized_sha, pre_selected_sha = _r3_population(pre_raw)
    expected_pre_count = conflict.get("occurrence_counts", {}).get(
        "normalized_unique_games"
    ) if isinstance(conflict.get("occurrence_counts"), Mapping) else None
    if (
        len(pre_games) != expected_pre_count
        or pre_normalized_sha != conflict.get("normalized_games_sha256")
        or pre_selected_sha != conflict.get("selected_raw_game_object_sha256s_sha256")
    ):
        raise ValueError("r3 pre-exclusion schedule population differs")
    try:
        missing_game = payload["dates"][_R3_MISSING_VENUE_POSITION[0]]["games"][
            _R3_MISSING_VENUE_POSITION[1]
        ]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("r3 missing-venue occurrence differs") from exc
    if (
        not isinstance(missing_game, Mapping)
        or missing_game.get("gamePk") != excluded_record.game_pk
        or canonical_json_bytes(missing_game)
        != canonical_json_bytes(_r3_plain(excluded.get("raw_object")))
        or hashlib.sha256(canonical_json_bytes(missing_game)).hexdigest()
        != excluded.get("raw_object_sha256")
    ):
        raise ValueError("r3 missing-venue occurrence differs")
    _r3_delete_occurrences(payload, (_R3_MISSING_VENUE_POSITION,))
    post_raw = canonical_json_bytes(payload)
    post_games, post_normalized_sha, post_selected_sha = _r3_population(post_raw)
    if (
        len(post_games) != len(pre_games) - 1
        or post_normalized_sha != _R3_POST_NORMALIZED_GAMES_SHA256
        or post_selected_sha != _R3_POST_SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256
        or excluded_record.game_pk in {game["game_pk"] for game in post_games}
    ):
        raise ValueError("r3 post-exclusion schedule population differs")
    return R3MissingVenueScheduleProjection(
        raw=post_raw,
        pre_normalized_games_count=len(pre_games),
        pre_normalized_games_sha256=pre_normalized_sha,
        pre_selected_raw_game_object_sha256s_sha256=pre_selected_sha,
        post_normalized_games_count=len(post_games),
        post_normalized_games_sha256=post_normalized_sha,
        post_selected_raw_game_object_sha256s_sha256=post_selected_sha,
        excluded_game_pks=projection.excluded_game_pks,
    )


def project_r3_conflict_schedule(
    raw: bytes, query: LogicalQuery, catalog: object
) -> tuple[bytes, Mapping[str, object]]:
    """Project one non-2021 r3 conflict query from its sealed record semantics."""
    from ..v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )

    if not isinstance(raw, bytes) or not isinstance(
        catalog, MissingVenueRecoveryDispositionCatalog
    ):
        raise ValueError("r3 conflict schedule authority differs")
    projection = catalog.as_projection()
    records = catalog.records_for_query(query)
    if (
        len(records) != 1
        or records[0].game_pk in projection.excluded_game_pks
        or logical_query_id(query)
        != projection.projection_query_id_for_game_pk(records[0].game_pk)
    ):
        raise ValueError("r3 conflict schedule projection differs")
    semantics = records[0].semantics
    suppressed = semantics.get("suppressed_occurrences")
    if suppressed is None:
        suppressed = [semantics.get("suppressed_occurrence")]
    if not isinstance(suppressed, (list, tuple)) or not suppressed:
        raise ValueError("r3 conflict suppressions differ")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("r3 conflict parent schedule differs") from exc
    if not isinstance(payload, dict):
        raise ValueError("r3 conflict parent schedule differs")
    positions: list[tuple[int, int]] = []
    for occurrence in suppressed:
        if not isinstance(occurrence, Mapping):
            raise ValueError("r3 conflict suppression differs")
        date_index = occurrence.get("date_index")
        game_index = occurrence.get("game_index")
        if type(date_index) is not int or type(game_index) is not int:
            raise ValueError("r3 conflict suppression differs")
        try:
            game = payload["dates"][date_index]["games"][game_index]
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("r3 conflict suppression differs") from exc
        if (
            not isinstance(game, Mapping)
            or hashlib.sha256(canonical_json_bytes(game)).hexdigest()
            != occurrence.get("sha256")
        ):
            raise ValueError("r3 conflict suppression differs")
        positions.append((date_index, game_index))
    _r3_delete_occurrences(payload, positions)
    filtered = canonical_json_bytes(payload)
    normalized, normalized_sha, selected_sha = _r3_population(filtered)
    counts = semantics.get("occurrence_counts")
    if (
        not isinstance(counts, Mapping)
        or len(normalized) != counts.get("normalized_unique_games")
        or normalized_sha != semantics.get("normalized_games_sha256")
        or selected_sha != semantics.get("selected_raw_game_object_sha256s_sha256")
    ):
        raise ValueError("r3 conflict schedule population differs")
    return filtered, counts


def _r3_rebound_catalog_and_parent_raw(
    query: LogicalQuery, catalog: object, context: object
) -> tuple[object, bytes]:
    """Rebind one r3 query and load its authenticated protected parent bytes."""
    from ..v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
        build_missing_venue_recovery_disposition_catalog,
    )

    repository_root = getattr(context, "repository_root", None)
    authority_path = getattr(context, "authority_path", None)
    contract_path = getattr(context, "acquisition_contract_path", None)
    if (
        not isinstance(catalog, MissingVenueRecoveryDispositionCatalog)
        or not isinstance(repository_root, Path)
        or not isinstance(authority_path, Path)
        or not isinstance(contract_path, Path)
    ):
        raise ValueError("r3 schedule disposition authority differs")
    rebound = build_missing_venue_recovery_disposition_catalog(
        repository_root, authority_path, contract_path
    )
    projection = rebound.as_projection()
    if (
        rebound != catalog
        or logical_query_id(query) not in projection.projection_query_ids
    ):
        raise ValueError("r3 schedule disposition authority differs")
    return rebound, _r3_parent_raw_from_rebound(query, rebound, context)


def _r3_parent_raw_from_rebound(
    query: LogicalQuery, catalog: object, context: object
) -> bytes:
    """Read the one catalog-selected protected parent after catalog rebinding."""
    from ..v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )
    from ..v52_recovery_parent_evidence import build_recovery_parent_catalog

    repository_root = getattr(context, "repository_root", None)
    if (
        not isinstance(catalog, MissingVenueRecoveryDispositionCatalog)
        or not isinstance(repository_root, Path)
    ):
        raise ValueError("r3 schedule disposition authority differs")
    records = catalog.records_for_query(query)
    parent_record = next((record for record in records if record.action is None), None)
    parent = (
        parent_record.semantics.get("parent_evidence")
        if parent_record is not None
        else None
    )
    raw_binding = parent.get("raw") if isinstance(parent, Mapping) else None
    raw_path = raw_binding.get("path") if isinstance(raw_binding, Mapping) else None
    if not isinstance(raw_path, str):
        raise ValueError("r3 schedule parent binding differs")
    configured_parent_root = os.environ.get(
        "MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT"
    ) or os.environ.get("MLB_V52_SOURCE_AUTHORITY_ROOT")
    parent_root = (
        Path(configured_parent_root).resolve(strict=True)
        if configured_parent_root
        else repository_root
    )
    parents = build_recovery_parent_catalog(parent_root)
    matches = tuple(
        name
        for name, authority in parents.parents.items()
        if authority.raw.relative.as_posix() == raw_path
    )
    if len(matches) != 1:
        raise ValueError("r3 schedule parent binding differs")
    return parents.read_raw(matches[0])


def _r3_missing_venue_projection_from_context(
    query: LogicalQuery, catalog: object, context: object
) -> R3MissingVenueScheduleProjection:
    rebound, parent_raw = _r3_rebound_catalog_and_parent_raw(query, catalog, context)
    return project_r3_missing_venue_schedule(parent_raw, query, rebound)


def _recompute_r3_schedule_anchor_sidecar(
    query: LogicalQuery, catalog: object, *, context: object
) -> Mapping[str, object]:
    """Rebuild an r3 derived sidecar from catalog-bound protected bytes only."""
    rebound, parent_raw = _r3_rebound_catalog_and_parent_raw(query, catalog, context)
    records = rebound.records_for_query(query)
    if len(records) == 2:
        return _recompute_schedule_anchor_sidecar(
            b"", query, context=context
        )
    if len(records) != 1:
        raise ValueError("r3 schedule projection record mapping differs")
    filtered, _counts = project_r3_conflict_schedule(parent_raw, query, rebound)
    return _recompute_schedule_anchor_sidecar(filtered, query, context=context)


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _official_optional_bool(
    record: Mapping[str, object], field: str, label: str
) -> bool:
    """MLB Stats API omits ordinary false site flags; explicit values stay strict."""
    value = record.get(field, False)
    if type(value) is not bool:
        raise ValueError(f"schedule {label} must be boolean when present")
    return value


def _raw_schedule_metadata(
    raw: bytes, *, allow_duplicate_game_pks: bool
) -> tuple[dict[int, dict[str, bool]], int, int]:
    payload = _json_object(raw, "schedule")
    total_games = payload.get("totalGames")
    if type(total_games) is not int or total_games < 0:
        raise ValueError("schedule totalGames must be a nonnegative integer")
    dates = payload.get("dates")
    if not isinstance(dates, list):
        raise ValueError("schedule dates must be a list")
    site_flags: dict[int, dict[str, bool]] = {}
    date_total_sum = 0
    for day in dates:
        if not isinstance(day, dict) or not isinstance(day.get("games"), list):
            raise ValueError("schedule date games must be a list")
        date_total = day.get("totalGames")
        if type(date_total) is not int or date_total < 0:
            raise ValueError("schedule date totalGames must be a nonnegative integer")
        date_total_sum += date_total
        for game in day["games"]:
            if not isinstance(game, dict) or game.get("gameType") != "R":
                continue
            game_pk = _positive(game.get("gamePk"), "schedule gamePk")
            neutral = _official_optional_bool(game, "neutralSite", "neutralSite")
            alternate = _official_optional_bool(
                game, "isAlternateSite", "isAlternateSite"
            )
            flags = {
                "alternate_site": alternate,
                "neutral_site": neutral,
            }
            if game_pk in site_flags and not allow_duplicate_game_pks:
                raise ValueError("schedule contains duplicate gamePk")
            prior = site_flags.setdefault(game_pk, flags)
            if prior != flags:
                raise ValueError("schedule duplicate gamePk site flags differ")
        if date_total != len(day["games"]):
            raise ValueError("schedule date totalGames differs from response population")
    raw_occurrences = parse_schedule_occurrences(raw)
    if total_games != date_total_sum or total_games != len(raw_occurrences):
        raise ValueError("schedule totalGames differs from exact parsed population")
    return site_flags, total_games, len(raw_occurrences)


def _schedule_range(
    query: LogicalQuery, *, allow_empty_parameters: bool = False
) -> tuple[int, date, date]:
    year = query.requested_range.get("year")
    if isinstance(year, bool) or not isinstance(year, int) or year not in range(2018, 2025):
        raise ValueError("schedule query year must be within 2018-2024")
    first = _day(query.requested_range.get("start_date"), "schedule query start")
    last = _day(query.requested_range.get("end_date"), "schedule query end")
    if first > last or first.year != year or last.year != year:
        raise ValueError("schedule query range differs from year")
    expected_parameters = {
        "sportId": 1,
        "gameType": "R",
        "startDate": first.isoformat(),
        "endDate": last.isoformat(),
        "hydrate": "probablePitcher,lineups,venue,linescore",
    }
    if dict(query.parameters) != expected_parameters and not (
        allow_empty_parameters and not query.parameters
    ):
        raise ValueError("schedule query parameters differ from requested range")
    return year, first, last


def _recompute_schedule_anchor_sidecar(
    raw: bytes,
    query: LogicalQuery,
    *,
    context: object | None = None,
    r3_projection: R3MissingVenueScheduleProjection | None = None,
) -> Mapping[str, object]:
    """Seal a complete schedule response without depending on boxscores."""
    allow_empty_parameters = False
    if query.parser_version == "v5.2.1":
        if context is not None:
            raise ValueError("context is only valid for a recovery schedule query")
        require_query(
            query,
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=_SCHEDULE_ENDPOINT,
            expected_media_type="application/json",
        )
    elif query.parser_version == "v5.2.2":
        if context is None:
            raise ValueError("recovery schedule context is required")
        from ..v52_query_plan import (
            _RECOVERY_DISPOSITION_ENDPOINTS,
            _load_static_contracts,
            _require_recovery_schedule_query,
        )

        authority_path = getattr(context, "authority_path", None)
        contract_path = getattr(context, "acquisition_contract_path", None)
        if not isinstance(authority_path, Path) or not isinstance(contract_path, Path):
            raise ValueError("recovery schedule context authority differs")
        rebound = _load_static_contracts(authority_path, contract_path)
        if rebound != context:
            raise ValueError("recovery schedule context authority differs")
        _require_recovery_schedule_query(query, rebound)
        allow_empty_parameters = (
            query.endpoint
            == _RECOVERY_DISPOSITION_ENDPOINTS.get(query.requested_range.get("year"))
        )
    elif query.parser_version == "v5.2.3":
        from ..v52_query_plan import (
            _R3_RECOVERY_RUN_ID,
            _load_static_contracts,
            build_static_batch,
        )

        authority_path = getattr(context, "authority_path", None)
        contract_path = getattr(context, "acquisition_contract_path", None)
        if not isinstance(authority_path, Path) or not isinstance(contract_path, Path):
            raise ValueError("r3 schedule context authority differs")
        rebound = _load_static_contracts(authority_path, contract_path)
        combined_projection = (
            query.endpoint
            == "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924"
        )
        canonical_projection = None
        if combined_projection:
            from ..v52_missing_venue_recovery_disposition import (
                build_missing_venue_recovery_disposition_catalog,
            )

            repository_root = getattr(context, "repository_root", None)
            if not isinstance(repository_root, Path):
                raise ValueError("r3 schedule context authority differs")
            canonical_catalog = build_missing_venue_recovery_disposition_catalog(
                repository_root, authority_path, contract_path
            )
            canonical_projection = project_r3_missing_venue_schedule(
                _r3_parent_raw_from_rebound(query, canonical_catalog, context),
                query,
                canonical_catalog,
            )
            if r3_projection is None:
                raw = canonical_projection.raw
                r3_projection = canonical_projection
        if (
            rebound != context
            or rebound.run_id != _R3_RECOVERY_RUN_ID
            or query not in build_static_batch(authority_path, contract_path).queries
            or (
                combined_projection
                and (
                    r3_projection is not None
                    and (
                        not isinstance(r3_projection, R3MissingVenueScheduleProjection)
                        or r3_projection != canonical_projection
                        or r3_projection.raw != raw
                    )
                )
            )
            or (not combined_projection and r3_projection is not None)
        ):
            raise ValueError("r3 schedule query authority differs")
        allow_empty_parameters = query.endpoint.startswith("disposition://")
    else:
        require_query(
            query,
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=_SCHEDULE_ENDPOINT,
            expected_media_type="application/json",
        )
    year, first, last = _schedule_range(
        query, allow_empty_parameters=allow_empty_parameters
    )
    site_flags, declared_total, raw_occurrence_count = _raw_schedule_metadata(
        raw, allow_duplicate_game_pks=query.parser_version in {"v5.2.2", "v5.2.3"}
    )
    validate_schedule_query_coverage(raw, first.isoformat(), last.isoformat())
    games = parse_schedule(raw)
    if not games:
        raise ValueError("schedule response contains no regular-season games")
    if raw_occurrence_count != declared_total:
        raise ValueError("schedule raw occurrence population differs from totalGames")
    if any(
        not first <= _day(game["official_date"], "game official_date") <= last
        for game in games
    ):
        raise ValueError("schedule game is outside query range")

    anchors = []
    for game in sorted(games, key=lambda row: int(row["game_pk"])):
        played = game["status"] in _PLAYED
        if played and (
            game["away_score"] is None
            or game["home_score"] is None
            or game["away_is_winner"] == game["home_is_winner"]
        ):
            raise ValueError("terminal schedule game lacks a unique outcome")
        marker = game["doubleheader_marker"]
        order = int(str(marker).removeprefix("GAME")) if marker else 1
        winner_team_id = None
        if played:
            winner_team_id = (
                game["away_team_id"]
                if game["away_is_winner"]
                else game["home_team_id"]
            )
            score_winner_team_id = (
                game["away_team_id"]
                if game["away_score"] > game["home_score"]
                else game["home_team_id"]
            )
            if (
                game["away_score"] == game["home_score"]
                or winner_team_id != score_winner_team_id
            ):
                raise ValueError("terminal schedule winner disagrees with final runs")
        anchor = {
            "away_score": game["away_score"],
            "alternate_site": site_flags[int(game["game_pk"])]["alternate_site"],
            "away_team_id": game["away_team_id"],
            "completed_innings": game["completed_innings"],
            "doubleheader_order": order,
            "game_pk": game["game_pk"],
            "home_score": game["home_score"],
            "home_team_id": game["home_team_id"],
            "neutral_site": site_flags[int(game["game_pk"])]["neutral_site"],
            "official_date": game["official_date"],
            "played": played,
            "scheduled_innings": game["scheduled_innings"],
            "scheduled_start_utc": game["scheduled_start_utc"],
            "season": year,
            "season_start_date": first.isoformat(),
            "terminal_status": game["status"],
            "venue_id": game["venue_id"],
            "winner_team_id": winner_team_id,
        }
        if anchor["venue_id"] is None:
            raise ValueError("regular-season schedule game requires official venue identity")
        anchors.append(anchor)

    game_pks = [int(row["game_pk"]) for row in anchors]
    played_pks = [int(row["game_pk"]) for row in anchors if row["played"]]
    unplayed_pks = [int(row["game_pk"]) for row in anchors if not row["played"]]
    semantics = {
        "anchors": anchors,
        "child_query_requirements": {
            "boxscores": [
                {
                    "endpoint": f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
                    "game_pk": game_pk,
                }
                for game_pk in played_pks
            ],
            "venues": [
                {
                    "endpoint": f"https://statsapi.mlb.com/api/v1/venues/{venue_id}",
                    "venue_id": venue_id,
                }
                for venue_id in sorted({int(row["venue_id"]) for row in anchors})
            ],
        },
        "coverage": {
            "inclusive_end": last.isoformat(),
            "inclusive_start": first.isoformat(),
            "season": year,
        },
        "fields_present": sorted(anchors[0]) if anchors else [],
        "game_pks": game_pks,
        "played_game_pks": played_pks,
        "unique_game_count": len(game_pks),
        "unplayed_game_pks": unplayed_pks,
    }
    if r3_projection is not None:
        semantics["recovery_disposition"] = {
            "excluded_game_pks": sorted(r3_projection.excluded_game_pks),
            "pre_normalized_games_count": r3_projection.pre_normalized_games_count,
            "pre_normalized_games_sha256": r3_projection.pre_normalized_games_sha256,
            "pre_selected_raw_game_object_sha256s_sha256": (
                r3_projection.pre_selected_raw_game_object_sha256s_sha256
            ),
            "post_normalized_games_count": r3_projection.post_normalized_games_count,
            "post_normalized_games_sha256": r3_projection.post_normalized_games_sha256,
            "post_selected_raw_game_object_sha256s_sha256": (
                r3_projection.post_selected_raw_game_object_sha256s_sha256
            ),
        }
    return query_sidecar(raw, query, semantics)


def recompute_schedule_anchor_sidecar(
    raw: bytes,
    query: LogicalQuery,
    *,
    context: object | None = None,
) -> Mapping[str, object]:
    """Seal a public schedule response without caller-supplied r3 projection state."""
    if (
        query.parser_version == "v5.2.3"
        and query.endpoint.startswith("disposition://")
    ):
        raise ValueError("r3 combined schedule requires sealed catalog projection")
    return _recompute_schedule_anchor_sidecar(raw, query, context=context)


def recompute_r3_missing_venue_schedule_anchor_sidecar(
    parent_raw: bytes,
    query: LogicalQuery,
    catalog: object,
    *,
    context: object,
) -> Mapping[str, object]:
    """Rebuild the one r3 derived sidecar from authenticated parent bytes only."""
    _rebound, canonical_parent_raw = _r3_rebound_catalog_and_parent_raw(
        query, catalog, context
    )
    if parent_raw != canonical_parent_raw:
        raise ValueError("r3 schedule parent raw differs")
    return _recompute_r3_schedule_anchor_sidecar(query, catalog, context=context)


def _boxscore_side(raw: object, label: str) -> tuple[int, int, list[int]]:
    if not isinstance(raw, dict):
        raise ValueError(f"boxscore {label} must be an object")
    team = raw.get("team")
    pitchers = raw.get("pitchers")
    players = raw.get("players")
    if not isinstance(team, dict):
        raise ValueError(f"boxscore {label} team must be an object")
    team_id = _positive(team.get("id"), f"boxscore {label} team id")
    if not isinstance(pitchers, list) or not pitchers:
        raise ValueError(f"boxscore {label} requires an actual starting pitcher")
    starter = _positive(pitchers[0], f"boxscore {label} starter")
    if not isinstance(players, dict):
        raise ValueError(f"boxscore {label} players must be an object")
    hitters: dict[int, int] = {}
    for player in players.values():
        if not isinstance(player, dict):
            raise ValueError(f"boxscore {label} player must be an object")
        order = player.get("battingOrder")
        if order is None:
            continue
        if not isinstance(order, str) or not order.isdecimal():
            raise ValueError(f"boxscore {label} battingOrder must be decimal")
        if order not in _STARTING_BATTING_ORDERS:
            continue
        slot = int(order)
        if slot in hitters:
            raise ValueError(f"boxscore {label} starting lineup is ambiguous")
        person = player.get("person")
        if not isinstance(person, dict):
            raise ValueError(f"boxscore {label} person must be an object")
        hitters[slot] = _positive(person.get("id"), f"boxscore {label} hitter")
    if set(hitters) != set(range(100, 1000, 100)) or len(set(hitters.values())) != 9:
        raise ValueError(f"boxscore {label} must contain nine unique starting hitters")
    return team_id, starter, [hitters[slot] for slot in sorted(hitters)]


def _boxscore(raw: bytes) -> dict[str, object]:
    payload = _json_object(raw, "boxscore")
    teams = payload.get("teams")
    if not isinstance(teams, dict) or set(teams) != {"away", "home"}:
        raise ValueError("boxscore must contain exactly away and home teams")
    away_team, away_starter, away_hitters = _boxscore_side(teams["away"], "away")
    home_team, home_starter, home_hitters = _boxscore_side(teams["home"], "home")
    if away_team == home_team or away_starter == home_starter:
        raise ValueError("boxscore sides must have distinct teams and starters")
    def runs(side: object, label: str) -> int:
        team_stats = side.get("teamStats") if isinstance(side, dict) else None
        batting = team_stats.get("batting") if isinstance(team_stats, dict) else None
        value = batting.get("runs") if isinstance(batting, dict) else None
        if type(value) is not int or value < 0:
            raise ValueError(f"boxscore {label} requires nonnegative terminal runs")
        return value

    away_score = runs(teams["away"], "away")
    home_score = runs(teams["home"], "home")
    if away_score == home_score:
        raise ValueError("boxscore terminal winner requires unequal final runs")
    winner_team_id = away_team if away_score > home_score else home_team
    return {
        "actual_starter_ids": [away_starter, home_starter],
        "away_starting_hitter_ids": away_hitters,
        "away_team_id": away_team,
        "home_starting_hitter_ids": home_hitters,
        "home_team_id": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "winner_team_id": winner_team_id,
    }


def recompute_boxscore_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    """Seal exactly one response-local boxscore participant projection."""
    require_query(
        query,
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        expected_media_type="application/json",
    )
    game_pk = _positive(query.requested_range.get("game_pk"), "boxscore game_pk")
    expected_endpoint = (
        f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    )
    if query.endpoint != expected_endpoint:
        raise ValueError("boxscore endpoint differs from requested gamePk")
    season = query.requested_range.get("season")
    if isinstance(season, bool) or not isinstance(season, int) or season not in range(2018, 2025):
        raise ValueError("boxscore season must be within 2018-2024")
    official = _day(query.requested_range.get("official_date"), "boxscore official date")
    if official.year != season:
        raise ValueError("boxscore official date differs from season")
    return query_sidecar(
        raw,
        query,
        {"game_pk": game_pk, "official_date": official.isoformat(), **_boxscore(raw)},
    )


def derived_schedule_sidecar_adapter(
    query: LogicalQuery,
    source_anchors: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    """Adapt source-owned schedule anchors into the rawless reducer envelope."""
    if isinstance(source_anchors, (str, bytes)) or not isinstance(
        source_anchors, Sequence
    ):
        raise ValueError("source schedule anchors must be a sequence")
    anchors: list[dict[str, object]] = []
    seen: set[int] = set()
    for raw_anchor in source_anchors:
        if not isinstance(raw_anchor, Mapping):
            raise ValueError("source schedule anchor must be an object")
        anchor = dict(raw_anchor)
        if set(anchor) != _SOURCE_SCHEDULE_ANCHOR_KEYS:
            raise ValueError("source schedule anchor keys differ")
        game_pk = _positive(anchor.get("game_pk"), "source schedule anchor gamePk")
        if game_pk in seen:
            raise ValueError("source schedule anchors contain duplicate gamePk")
        seen.add(game_pk)
        away_team_id = _positive(
            anchor.get("away_team_id"), "source schedule away team ID"
        )
        home_team_id = _positive(
            anchor.get("home_team_id"), "source schedule home team ID"
        )
        _positive(anchor.get("venue_id"), "source schedule venue ID")
        _positive(
            anchor.get("doubleheader_order"), "source schedule doubleheader order"
        )
        if (
            not isinstance(anchor.get("raw_game_object_sha256"), str)
            or _SHA256.fullmatch(anchor["raw_game_object_sha256"]) is None
            or type(anchor.get("played")) is not bool
            or type(anchor.get("neutral_site")) is not bool
            or type(anchor.get("alternate_site")) is not bool
            or away_team_id == home_team_id
        ):
            raise ValueError("source schedule anchor values differ")
        season = anchor.get("season")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in range(2018, 2025)
        ):
            raise ValueError("source schedule anchor season differs")
        _day(anchor.get("official_date"), "source schedule official date")
        _day(anchor.get("season_start_date"), "source schedule season start date")
        if season == 2019:
            anchors.append(
                {
                    key: value
                    for key, value in anchor.items()
                    if key != "raw_game_object_sha256"
                }
            )
    if not anchors:
        raise ValueError("source schedule adapter requires 2019 anchors")
    return {
        "byte_count": 0,
        "grain": query.grain,
        "parser_version": query.parser_version,
        "query_id": logical_query_id(query),
        "raw_sha256": hashlib.sha256(b"").hexdigest(),
        "schema_version": 1,
        "semantics": {"anchors": anchors},
        "source": query.source,
    }


def recompute_game_evidence_snapshot(
    schedule_anchors: Sequence[Mapping[str, object]],
    boxscores: Mapping[int, tuple[bytes, str]],
    *,
    schedule_provenance: Mapping[int, Mapping[str, object]] | None = None,
) -> Mapping[str, object]:
    """Join verified schedule anchors with per-played-game boxscore evidence."""
    if isinstance(schedule_anchors, (str, bytes)) or not isinstance(
        schedule_anchors, Sequence
    ):
        raise ValueError("schedule_anchors must be a sequence")
    if not isinstance(boxscores, Mapping):
        raise ValueError("boxscores must be a mapping")
    if not schedule_anchors:
        raise ValueError("schedule game snapshot requires a nonempty anchor population")
    anchors_by_pk: dict[int, tuple[dict[str, object], str]] = {}
    sidecar_hashes: list[str] = []
    for sidecar in schedule_anchors:
        if (
            not isinstance(sidecar, Mapping)
            or set(sidecar) != QUERY_SIDECAR_KEYS
            or sidecar.get("source") != "mlb_schedule_game_feed"
            or sidecar.get("grain") != "season_schedule_and_game"
            or sidecar.get("parser_version") != "v5.2.1"
            or sidecar.get("schema_version") != 1
            or type(sidecar.get("byte_count")) is not int
            or sidecar["byte_count"] < 0
            or not isinstance(sidecar.get("query_id"), str)
            or not _SHA256.fullmatch(sidecar["query_id"])
            or not isinstance(sidecar.get("raw_sha256"), str)
            or not _SHA256.fullmatch(sidecar["raw_sha256"])
        ):
            raise ValueError("schedule anchor must be a canonical Task2 query sidecar")
        semantics = sidecar.get("semantics")
        if not isinstance(semantics, Mapping) or not isinstance(
            semantics.get("anchors"), list
        ):
            raise ValueError("schedule anchor semantics are invalid")
        sidecar_hash = hashlib.sha256(
            canonical_json_bytes(dict(sidecar)) + b"\n"
        ).hexdigest()
        sidecar_hashes.append(sidecar_hash)
        for raw_anchor in semantics["anchors"]:
            if not isinstance(raw_anchor, dict):
                raise ValueError("schedule anchor must be an object")
            game_pk = _positive(raw_anchor.get("game_pk"), "anchor game_pk")
            if (
                type(raw_anchor.get("played")) is not bool
                or type(raw_anchor.get("neutral_site")) is not bool
                or type(raw_anchor.get("alternate_site")) is not bool
            ):
                raise ValueError("schedule anchor disposition flags must be explicit")
            if game_pk in anchors_by_pk:
                raise ValueError("schedule snapshots contain duplicate gamePk")
            anchors_by_pk[game_pk] = (dict(raw_anchor), sidecar_hash)

    if not anchors_by_pk:
        raise ValueError("schedule game snapshot requires a nonempty game population")
    if schedule_provenance is not None:
        if not isinstance(schedule_provenance, Mapping) or set(schedule_provenance) != set(
            anchors_by_pk
        ):
            raise ValueError("schedule provenance population differs")
        for game_pk, (anchor, _anchor_hash) in anchors_by_pk.items():
            provenance = schedule_provenance.get(game_pk)
            if not isinstance(provenance, Mapping) or set(provenance) != {
                "game_pk",
                "official_date",
                "away_team_id",
                "home_team_id",
                "raw_game_object_sha256",
                "normalized_anchor_sha256",
            }:
                raise ValueError("schedule provenance keys differ")
            if any(
                provenance.get(field) != anchor.get(field)
                for field in ("game_pk", "official_date", "away_team_id", "home_team_id")
            ):
                raise ValueError("schedule provenance identity differs")
            for field in ("raw_game_object_sha256", "normalized_anchor_sha256"):
                value = provenance.get(field)
                if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                    raise ValueError("schedule provenance hash differs")

    played_anchor_pks = {
        game_pk for game_pk, (anchor, _hash) in anchors_by_pk.items()
        if anchor.get("played") is True
    }
    extra = set(boxscores) - played_anchor_pks
    if extra:
        raise ValueError("boxscore has no verified schedule anchor")
    verified_boxscores: dict[int, tuple[dict[str, object], str]] = {}
    for game_pk, evidence in boxscores.items():
        if (
            not isinstance(evidence, tuple)
            or len(evidence) != 2
            or not isinstance(evidence[0], bytes)
            or not isinstance(evidence[1], str)
            or hashlib.sha256(evidence[0]).hexdigest() != evidence[1]
        ):
            raise ValueError("boxscore bytes do not match their verified hash")
        try:
            decoded = json.loads(evidence[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("boxscore sidecar artifact must be canonical JSON") from exc
        if (
            not isinstance(decoded, dict)
            or set(decoded) != QUERY_SIDECAR_KEYS
            or decoded.get("source") != "mlb_schedule_game_feed"
            or decoded.get("grain") != "season_schedule_and_game"
            or decoded.get("parser_version") != "v5.2.1"
            or decoded.get("schema_version") != 1
            or type(decoded.get("byte_count")) is not int
            or decoded["byte_count"] < 0
            or not isinstance(decoded.get("query_id"), str)
            or not _SHA256.fullmatch(decoded["query_id"])
            or not isinstance(decoded.get("raw_sha256"), str)
            or not _SHA256.fullmatch(decoded["raw_sha256"])
            or canonical_json_bytes(decoded) + b"\n" != evidence[0]
            or not isinstance(decoded.get("semantics"), dict)
            or decoded["semantics"].get("game_pk") != game_pk
        ):
            raise ValueError("boxscore query sidecar lineage differs from gamePk")
        verified_boxscores[game_pk] = (decoded, evidence[1])
        sidecar_hashes.append(evidence[1])
    games = []
    for game_pk, (anchor, anchor_hash) in sorted(anchors_by_pk.items()):
        played = anchor.get("played") is True
        base = {
            **anchor,
            "feature_history_cutoff": (
                _day(anchor.get("official_date"), "anchor official_date")
                - timedelta(days=1)
            ).isoformat(),
        }
        if not played:
            game = {
                **base,
                "actual_starter_ids": [],
                "away_starting_hitter_ids": [],
                "home_starting_hitter_ids": [],
                "participant_provenance": "HISTORICAL_NO_PARTICIPANTS",
                "schedule_query_sidecar_sha256": anchor_hash,
                "verification_status": "INELIGIBLE_UNPLAYED",
            }
            game["source_game_sha256"] = canonical_sha256(
                {"anchor_sidecar_sha256": anchor_hash, "game": game}
            )
            games.append(game)
            continue
        evidence = verified_boxscores.get(game_pk)
        if evidence is None:
            raise ValueError(f"missing boxscore for played game {game_pk}")
        participants = dict(evidence[0]["semantics"])
        if (
            participants["away_team_id"] != anchor.get("away_team_id")
            or participants["home_team_id"] != anchor.get("home_team_id")
        ):
            raise ValueError("boxscore team identity disagrees with schedule anchor")
        if any(
            participants.get(field) != anchor.get(field)
            for field in (
                "official_date",
                "away_score",
                "home_score",
                "winner_team_id",
            )
        ):
            raise ValueError("boxscore terminal evidence disagrees with schedule anchor")
        participants.pop("game_pk", None)
        participants.pop("official_date", None)
        game = {
            **base,
            **participants,
            "boxscore_query_sidecar_sha256": evidence[1],
            "schedule_query_sidecar_sha256": anchor_hash,
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "verification_status": "VERIFIED",
        }
        if schedule_provenance is not None:
            provenance = schedule_provenance[game_pk]
            game["raw_game_object_sha256"] = provenance["raw_game_object_sha256"]
            game["normalized_anchor_sha256"] = provenance[
                "normalized_anchor_sha256"
            ]
        game["source_game_sha256"] = canonical_sha256(
            {
                "anchor_sidecar_sha256": anchor_hash,
                "boxscore_query_sidecar_sha256": evidence[1],
                "game": game,
            }
        )
        games.append(game)
    return {
        "games": games,
        "grain": "season_game_evidence_snapshot",
        "schema_version": 1,
        "source_query_sidecar_sha256s": sorted(set(sidecar_hashes)),
    }


__all__ = [
    "R3MissingVenueScheduleProjection",
    "derived_schedule_sidecar_adapter",
    "project_r3_conflict_schedule",
    "project_r3_missing_venue_schedule",
    "recompute_boxscore_sidecar",
    "recompute_game_evidence_snapshot",
    "recompute_r3_missing_venue_schedule_anchor_sidecar",
    "recompute_schedule_anchor_sidecar",
]
