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

R4_PHYSICAL_CUSTODY_RECEIPT_PATH = (
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
    "acquisition/physical_custody_transition_r4.json"
)
R4_PHYSICAL_CUSTODY_INVENTORY_PATH = (
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
    "acquisition/protected_raw_inventory_r4.json"
)
R4_SOURCE_APPROVAL = {
    "logical_raw_root": "data/v5_1_bt/raw",
    "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json",
    "protected_inventory_sha256": (
        "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6"
    ),
}


def parse_r4_physical_custody_transition(
    value: object, *, profile: object | None = None
) -> dict[str, object]:
    """Parse the acyclic R4 contract-to-custody binding without a hash cycle."""
    transition = _closed_r4_mapping(
        value,
        {"receipt", "source_approval", "r4_inventory"},
        "r4 physical custody transition",
    )
    receipt = _closed_r4_mapping(transition["receipt"], {"path", "sha256"}, "r4 receipt")
    inventory = _closed_r4_mapping(
        transition["r4_inventory"], {"path", "sha256"}, "r4 inventory"
    )
    expected_approval = R4_SOURCE_APPROVAL
    if profile is not None:
        approval = getattr(profile, "source_approval", None)
        if not isinstance(approval, Mapping):
            raise ValueError("r4 source authority profile differs")
        expected_approval = dict(approval)
    source_approval = _closed_r4_mapping(
        transition["source_approval"], set(expected_approval), "r4 source approval"
    )
    if (
        receipt["path"] != R4_PHYSICAL_CUSTODY_RECEIPT_PATH
        or inventory["path"] != R4_PHYSICAL_CUSTODY_INVENTORY_PATH
        or source_approval != expected_approval
    ):
        raise ValueError("r4 physical custody transition differs")
    _r4_sha256(receipt["sha256"], "r4 receipt")
    _r4_sha256(inventory["sha256"], "r4 inventory")
    return {
        "receipt": dict(receipt),
        "source_approval": dict(source_approval),
        "r4_inventory": dict(inventory),
    }


def validate_r4_recovery_acquisition_contract(
    contract: Mapping[str, object], bindings: Mapping[str, object], *, profile: object | None = None
) -> None:
    """Public strict R4 validator; default authority remains the fixed source."""
    if profile is None:
        from .v52_r4_authority import production_source_authority_profile

        profile = production_source_authority_profile()
    expected_keys = {
        "bindings", "canonical_roots", "forbidden_years", "host_concurrency",
        "legacy_protection", "minimum_free_space_reserve_bytes",
        "physical_custody_transition", "recovery_disposition", "recovery_provenance",
        "request_timeout_seconds", "required_years", "response_byte_limits",
        "response_completeness_limits", "run_id", "schema_version",
        "season_boundaries", "sources", "timezone_mapping_path",
    }
    expected_bindings = {
        "approved_design": {
            "path": "docs/superpowers/specs/2026-08-04-v5-2-r4-physical-custody-authority-design.md",
            "sha256": "372e001466d1d1fa3109307e0fd69784110abb8e2db71509bbe68447fad41663",
            "git_blob_id": "81c8587ccb433be156b859dc8a7eb170241ca06c",
        },
        "implementation_plan": {
            "path": "docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md",
            "working_tree_sha256": "a8ee2e245fb9a446099dfd862472e72f4f4d77ee9e5850d27a098f60ebebc1d2",
            "git_blob_id": "f6e0cef0b4689ee5c45d495b3c89b46d861a257f",
        },
        "run_spec": {
            "path": "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json",
            "sha256": "4e49bd9a261acb2237d9a0a5712568ec0e236eda2a1427f7a4035d195e242b52",
            "git_blob_id": "d48909ded446d68ace15f6dc927a071808ef9a4a",
        },
    }
    expected_roots = {
        "raw": "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw",
        "sources": "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources",
        "acquisition_audit": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition",
        "execution_audit": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/execution",
    }
    expected_disposition = {
        "parser_version": "v5.2.3", "policy_version": "v5.2.3",
        "record_game_pks": [565534, 630882, 633224, 632924],
        "projection_endpoints": [
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
            "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924",
        ],
        "record_to_projection_indexes": [0, 1, 2, 2], "excluded_game_pks": [632924],
    }
    if (
        set(contract) != expected_keys
        or contract.get("run_id") != "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4"
        or contract.get("schema_version") != 2
        or contract.get("canonical_roots") != expected_roots
        or dict(bindings) != expected_bindings
        or contract.get("recovery_disposition") != expected_disposition
        or canonical_sha256(contract.get("recovery_provenance"))
        != "b33b1e438f5a8faaac916d352481b670f6a7323248385f55f3bd85f54a64067f"
    ):
        raise ValueError("r4 recovery acquisition authority differs")
    transition = parse_r4_physical_custody_transition(
        contract.get("physical_custody_transition"), profile=profile
    )
    protection = contract.get("legacy_protection")
    if (
        not isinstance(protection, Mapping)
        or set(protection)
        != {
            "baseline_inventory_path", "baseline_inventory_sha256", "legacy_root",
            "permitted_years", "protected_inventory_path", "protected_inventory_sha256",
        }
        or transition["r4_inventory"]
        != {
            "path": protection.get("protected_inventory_path"),
            "sha256": protection.get("protected_inventory_sha256"),
        }
    ):
        raise ValueError("r4 inventory must be active legacy protection")
    expected_schedule = {
        "name": "mlb_schedule_game_feed", "grain": "season_schedule_and_game",
        "parser_version": "v5.2.3", "endpoints": [
            "https://statsapi.mlb.com/api/v1/schedule",
            "https://statsapi.mlb.com/api/v1/game/{gamePk}/boxscore",
            *expected_disposition["projection_endpoints"],
        ],
    }
    sources = contract.get("sources")
    if not isinstance(sources, list) or len(sources) != 6 or expected_schedule not in sources:
        raise ValueError("r4 recovery schedule source differs")


def _closed_r4_mapping(
    value: object, keys: set[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} differs")
    return value


def _r4_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} SHA-256 differs")
    return value


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


def recompute_query_sidecar(
    query: LogicalQuery,
    raw: bytes,
    *,
    authority: object | None = None,
    context: object | None = None,
) -> Mapping[str, object]:
    """Dispatch one raw response to its exact grain-specific recomputer."""
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    key = (query.source, query.grain)
    if (
        key == ("mlb_schedule_game_feed", "season_schedule_and_game")
        and query.parser_version == "v5.2.3"
        and query.endpoint.startswith("disposition://")
    ):
        from .v52_missing_venue_recovery_disposition import (
            MissingVenueRecoveryDispositionCatalog,
        )
        from .v52_sources.schedule import _recompute_r3_schedule_anchor_sidecar

        if (
            raw != b""
            or not isinstance(authority, MissingVenueRecoveryDispositionCatalog)
            or context is None
            or logical_query_id(query)
            not in authority.as_projection().projection_query_ids
        ):
            raise ValueError("r3 schedule disposition authority differs")
        return _bare_semantics(
            _recompute_r3_schedule_anchor_sidecar(
                query, authority, context=context
            )
        )
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
            _recovery_sidecar_semantics_from_authenticated,
            authenticate_recovery_disposition_authority,
            is_canonical_recovery_disposition_authority,
        )

        if not is_canonical_recovery_disposition_authority(authority):
            raise TypeError(
                "recovery schedule disposition authority must be canonical"
            )
        if authority.query != query or authority.semantics.query != query:
            raise ValueError("recovery schedule disposition authority query differs")
        if raw != b"":
            raise ValueError("recovery schedule disposition must not claim raw bytes")
        rebound = authenticate_recovery_disposition_authority(authority)
        return _recovery_sidecar_semantics_from_authenticated(rebound)
    if authority is not None:
        raise ValueError("authority is only valid for the schedule disposition")
    if key == ("mlb_schedule_game_feed", "season_schedule_and_game"):
        from .v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_schedule_anchor_sidecar,
        )

        if query.endpoint == "https://statsapi.mlb.com/api/v1/schedule":
            return _bare_semantics(
                recompute_schedule_anchor_sidecar(raw, query, context=context)
            )
        if query.endpoint.startswith("https://statsapi.mlb.com/api/v1/game/"):
            if context is not None:
                raise ValueError("context is only valid for a recovery schedule query")
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
    "parse_r4_physical_custody_transition",
    "validate_r4_recovery_acquisition_contract",
]
