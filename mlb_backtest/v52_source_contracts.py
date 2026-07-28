"""Closed v5.2 source-semantic contracts and query dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .v52_evidence import LogicalQuery, logical_query_id


QUERY_SIDECAR_KEYS = frozenset(
    {
        "byte_count",
        "grain",
        "parser_version",
        "query_id",
        "raw_sha256",
        "schema_version",
        "semantics",
        "source",
    }
)


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("source semantics must be canonical JSON compatible") from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def query_sidecar(
    raw: bytes, query: LogicalQuery, semantics: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if not isinstance(semantics, Mapping) or not all(
        isinstance(key, str) for key in semantics
    ):
        raise ValueError("semantics must be a string-keyed mapping")
    normalized = json.loads(canonical_json_bytes(dict(semantics)))
    return {
        "byte_count": len(raw),
        "grain": query.grain,
        "parser_version": query.parser_version,
        "query_id": logical_query_id(query),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": 1,
        "semantics": normalized,
        "source": query.source,
    }


def require_query(
    query: LogicalQuery,
    *,
    source: str,
    grain: str,
    endpoint: str | None = None,
    expected_media_type: str | None = None,
) -> None:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if query.source != source or query.grain != grain:
        raise ValueError(f"query must use {source}/{grain}")
    if endpoint is not None and query.endpoint != endpoint:
        raise ValueError("query endpoint differs from the source contract")
    if (
        expected_media_type is not None
        and query.expected_media_type != expected_media_type
    ):
        raise ValueError("query expected media type differs from the source contract")
    if query.parser_version != "v5.2.1":
        raise ValueError("query parser_version differs from v5.2.1")


def require_schedule_query(
    query: LogicalQuery,
    *,
    endpoint: str,
    expected_media_type: str,
) -> None:
    """Validate the schedule grain while leaving generic v5.2.1 closed."""
    if query.parser_version == "v5.2.1":
        require_query(
            query,
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=endpoint,
            expected_media_type=expected_media_type,
        )
        return
    if (
        query.parser_version != "v5.2.2"
        or query.source != "mlb_schedule_game_feed"
        or query.grain != "season_schedule_and_game"
        or query.endpoint != endpoint
        or query.expected_media_type != expected_media_type
    ):
        raise ValueError("schedule query authority differs")


def recompute_query_sidecar(
    query: LogicalQuery, raw: bytes, *, authority: object | None = None
) -> Mapping[str, object]:
    """Dispatch one raw response to its exact grain-specific recomputer."""
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    key = (query.source, query.grain)
    if (
        key == ("mlb_schedule_game_feed", "season_schedule_and_game")
        and query.endpoint
        == "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534"
        and query.parser_version == "v5.2.1"
    ):
        from .v52_duplicate_gamepk_disposition import (
            DuplicateGamePkDispositionAuthority,
            _sidecar_semantics,
        )

        if not isinstance(authority, DuplicateGamePkDispositionAuthority):
            raise TypeError("schedule disposition requires its authenticated authority")
        if authority.query != query:
            raise ValueError("schedule disposition authority query differs")
        if raw != b"":
            raise ValueError("schedule disposition must not claim raw bytes")
        rebound = authority.reauthenticate()
        projection = rebound.recompute()
        return _sidecar_semantics(rebound, projection)
    if (
        key == ("mlb_schedule_game_feed", "season_schedule_and_game")
        and query.endpoint
        in {
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
        }
    ):
        from .v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionAuthority,
        )

        if not isinstance(authority, RecoveryDispositionAuthority):
            raise TypeError(
                "recovery schedule disposition requires its authenticated authority"
            )
        if authority.query != query or authority.semantics.query != query:
            raise ValueError("recovery schedule disposition authority query differs")
        if raw != b"":
            raise ValueError("recovery schedule disposition must not claim raw bytes")
        rebound = authority.reauthenticate()
        if rebound != authority:
            raise ValueError("recovery schedule disposition authority rebind differs")
        return rebound.sidecar_semantics()
    if authority is not None:
        raise ValueError("authority is only valid for the schedule disposition")
    if key == ("mlb_schedule_game_feed", "season_schedule_and_game"):
        from .v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_schedule_anchor_sidecar,
        )

        if query.endpoint == "https://statsapi.mlb.com/api/v1/schedule":
            return _bare_semantics(recompute_schedule_anchor_sidecar(raw, query))
        if query.endpoint.startswith("https://statsapi.mlb.com/api/v1/game/"):
            return _bare_semantics(recompute_boxscore_sidecar(raw, query))
        raise ValueError("unsupported schedule/game endpoint")
    if key == ("baseball_savant_statcast", "pitch"):
        from .v52_sources.statcast import recompute_statcast_sidecar

        return _bare_semantics(recompute_statcast_sidecar(raw, query))
    if key == (
        "mlb_stats_pitching",
        "season_d1_league_pitching_reference",
    ):
        from .v52_sources.pitching import recompute_pitching_sidecar

        consumers = _requested_consumers(query)
        if query.endpoint == "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING":
            return _preseason_semantics(query, raw, consumers)
        return _bare_semantics(recompute_pitching_sidecar(raw, query, consumers))
    if key == (
        "mlb_stats_pitcher_bf",
        "player_career_batters_faced_reference",
    ):
        from .v52_sources.pitching import recompute_career_bf_sidecar

        consumers = _requested_consumers(query)
        return _bare_semantics(recompute_career_bf_sidecar(raw, query, consumers))
    if key == (
        "venue_team_timezone_table",
        "effective_dated_venue_team_reference",
    ):
        from .v52_sources.references import (
            recompute_team_query_sidecar,
            recompute_venue_query_sidecar,
        )

        if query.endpoint == "https://statsapi.mlb.com/api/v1/teams":
            return _bare_semantics(recompute_team_query_sidecar(raw, query))
        if query.endpoint.startswith("https://statsapi.mlb.com/api/v1/venues/"):
            return _bare_semantics(recompute_venue_query_sidecar(raw, query))
        raise ValueError("unsupported team/venue reference endpoint")
    raise ValueError("unsupported v5.2 source/grain dispatch")


def _bare_semantics(sidecar: Mapping[str, object]) -> Mapping[str, object]:
    if set(sidecar) != QUERY_SIDECAR_KEYS or not isinstance(
        sidecar.get("semantics"), Mapping
    ):
        raise ValueError("grain recomputer returned an invalid query sidecar envelope")
    return dict(sidecar["semantics"])


def _requested_consumers(query: LogicalQuery) -> list[int]:
    values = query.requested_range.get("consuming_game_pks")
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        raise ValueError("query must bind positive consuming_game_pks")
    if values != sorted(set(values)) or not values:
        raise ValueError("query consuming_game_pks must be nonempty, sorted, and unique")
    return values


def _preseason_semantics(
    query: LogicalQuery, raw: bytes, consumers: list[int]
) -> Mapping[str, object]:
    require_query(
        query,
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint="disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
        expected_media_type="application/json",
    )
    if raw != b"" or query.parameters:
        raise ValueError("preseason disposition must not claim HTTP response bytes")
    season = query.requested_range.get("season")
    as_of = query.requested_range.get("as_of_date")
    cutoffs = query.requested_range.get("consumer_cutoff_dates")
    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season not in range(2018, 2025)
        or not isinstance(as_of, str)
        or not isinstance(cutoffs, Mapping)
        or cutoffs != {str(game_pk): as_of for game_pk in consumers}
        or query.requested_range.get("disposition")
        != "PRESEASON_NO_CURRENT_SEASON_PITCHING"
    ):
        raise ValueError("preseason disposition consumer cutoff binding differs")
    return {
        "as_of_date": as_of,
        "consumer_cutoff_dates": dict(cutoffs),
        "consuming_game_pks": consumers,
        "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        "season": season,
    }


__all__ = [
    "QUERY_SIDECAR_KEYS",
    "canonical_json_bytes",
    "canonical_sha256",
    "query_sidecar",
    "recompute_query_sidecar",
    "require_query",
    "require_schedule_query",
]
