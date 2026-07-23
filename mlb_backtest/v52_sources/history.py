"""Leakage-safe prior-schedule projections from verified game evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from types import MappingProxyType

from ..v52_source_contracts import canonical_sha256


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
EXPECTED_NEUTRALIZATIONS: tuple[tuple[str, str, int, str, int], ...] = (
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156,
        "2019-09-06",
        116,
    ),
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156,
        "2019-09-06",
        133,
    ),
    (
        "92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef",
        "5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96",
        565338,
        "2019-05-20",
        133,
    ),
)
KNOWN_UNAPPROVED_NEIGHBORS: tuple[tuple[str, str, int, str, int], ...] = (
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535,
        "2019-05-21",
        116,
    ),
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535,
        "2019-05-21",
        146,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157,
        "2019-09-07",
        116,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157,
        "2019-09-07",
        133,
    ),
)

_NEUTRALIZATION_REASON = "schedule_neutral_duplicate_gamepk_dependency"
_NEUTRALIZATION_KEYS = frozenset(
    {
        "game_pk",
        "official_date",
        "team_id",
        "target_raw_game_object_sha256",
        "target_normalized_anchor_sha256",
        "value",
        "sample_size",
        "quality",
        "reason_codes",
    }
)
_SOURCE_ANCHOR_KEYS = frozenset(
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
TASK10_DISPOSITION_QUERY_ID = (
    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
)
TASK10_DISPOSITION_SIDECAR_SHA256 = (
    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1"
)
TASK10_DISPOSITION_SIDECAR_PATH = (
    "audit/v5_2_qd/historical_superseding_565534/acquisition/query_sidecars/"
    f"{TASK10_DISPOSITION_SIDECAR_SHA256}.json"
)
TASK10_DISPOSITION_SUCCESS_PATH = (
    "data/v5_2_qd/historical_superseding_565534/raw/queries/"
    f"mlb_schedule_game_feed/{TASK10_DISPOSITION_QUERY_ID}/success.json"
)
TASK10_DISPOSITION_SUCCESS_SHA256 = "8" * 64
TASK10_DISPOSITION_BINDING = MappingProxyType(
    {
        "query_id": TASK10_DISPOSITION_QUERY_ID,
        "query_sidecar": MappingProxyType(
            {
                "path": TASK10_DISPOSITION_SIDECAR_PATH,
                "sha256": TASK10_DISPOSITION_SIDECAR_SHA256,
            }
        ),
        "success_receipt": MappingProxyType(
            {
                "path": TASK10_DISPOSITION_SUCCESS_PATH,
                "sha256": TASK10_DISPOSITION_SUCCESS_SHA256,
            }
        ),
    }
)


def _expected_neutralization_records() -> tuple[Mapping[str, object], ...]:
    return tuple(
        MappingProxyType(
            {
                "game_pk": game_pk,
                "official_date": official_date,
                "team_id": team_id,
                "target_raw_game_object_sha256": raw_sha256,
                "target_normalized_anchor_sha256": anchor_sha256,
                "value": 50.0,
                "sample_size": 0,
                "quality": 0.0,
                "reason_codes": [_NEUTRALIZATION_REASON],
            }
        )
        for raw_sha256, anchor_sha256, game_pk, official_date, team_id in EXPECTED_NEUTRALIZATIONS
    )


def validate_schedule_neutralizations(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    """Accept only empty legacy input or the exact canonical policy records."""
    if value is None or value == () or value == []:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("schedule neutralizations must be a sequence")
    records: list[Mapping[str, object]] = []
    identities: list[tuple[str, str, int, str, int]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _NEUTRALIZATION_KEYS:
            raise ValueError("schedule neutralization record keys differ")
        raw_hash = raw.get("target_raw_game_object_sha256")
        anchor_hash = raw.get("target_normalized_anchor_sha256")
        game_pk = raw.get("game_pk")
        official_date = raw.get("official_date")
        team_id = raw.get("team_id")
        if (
            not isinstance(raw_hash, str)
            or _SHA256.fullmatch(raw_hash) is None
            or not isinstance(anchor_hash, str)
            or _SHA256.fullmatch(anchor_hash) is None
            or isinstance(game_pk, bool)
            or not isinstance(game_pk, int)
            or game_pk <= 0
            or isinstance(team_id, bool)
            or not isinstance(team_id, int)
            or team_id <= 0
        ):
            raise ValueError("schedule neutralization identity differs")
        _day(official_date, "schedule neutralization official date")
        if (
            type(raw.get("value")) is not float
            or raw.get("value") != 50.0
            or type(raw.get("sample_size")) is not int
            or raw.get("sample_size") != 0
            or type(raw.get("quality")) is not float
            or raw.get("quality") != 0.0
            or raw.get("reason_codes") != [_NEUTRALIZATION_REASON]
        ):
            raise ValueError("schedule neutralization metric differs")
        identity = (raw_hash, anchor_hash, game_pk, official_date, team_id)
        identities.append(identity)
        records.append(MappingProxyType(dict(raw)))
    if (
        len(identities) != len(set(identities))
        or identities != sorted(identities, key=lambda item: (item[0], item[2], item[4]))
        or tuple(identities) != EXPECTED_NEUTRALIZATIONS
    ):
        raise ValueError("schedule neutralization policy differs")
    return tuple(records)


def validate_task10_disposition_binding(value: object) -> None:
    """Require the one immutable Task10 duplicate-game disposition binding."""
    if not isinstance(value, Mapping) or value != TASK10_DISPOSITION_BINDING:
        raise ValueError("duplicate gamePk disposition differs")


def _source_anchor(game: Mapping[str, object]) -> dict[str, object]:
    if not _SOURCE_ANCHOR_KEYS.issubset(game):
        raise ValueError("source game provenance is incomplete")
    anchor = {key: game[key] for key in _SOURCE_ANCHOR_KEYS}
    raw_hash = anchor["raw_game_object_sha256"]
    normalized_hash = game.get("normalized_anchor_sha256")
    if (
        not isinstance(raw_hash, str)
        or _SHA256.fullmatch(raw_hash) is None
        or not isinstance(normalized_hash, str)
        or _SHA256.fullmatch(normalized_hash) is None
        or canonical_sha256(anchor) != normalized_hash
    ):
        raise ValueError("source game provenance differs")
    return anchor


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


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


def recompute_prior_history_sidecar(
    schedule_sidecar: Mapping[str, object],
    *,
    source_snapshot_sha256: str,
    run_id: str,
    duplicate_gamepk_disposition: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    if (
        not isinstance(source_snapshot_sha256, str)
        or _SHA256.fullmatch(source_snapshot_sha256) is None
    ):
        raise ValueError("source snapshot SHA-256 differs")
    if not isinstance(schedule_sidecar, Mapping):
        raise ValueError("schedule_sidecar must be a mapping")
    if schedule_sidecar.get("grain") != "season_game_evidence_snapshot":
        raise ValueError("prior history requires a season game-evidence snapshot")
    raw_games = schedule_sidecar.get("games")
    if isinstance(raw_games, (str, bytes)) or not isinstance(raw_games, Sequence):
        raise ValueError("game-evidence snapshot games must be a sequence")
    if not raw_games:
        raise ValueError("prior history requires a nonempty game population")
    games = []
    source_hashes = []
    seen: set[int] = set()
    source_2019_anchors: dict[int, dict[str, object]] = {}
    source_seasons: set[int] = set()
    for raw in raw_games:
        if not isinstance(raw, Mapping):
            raise ValueError("game evidence rows must be mappings")
        game_pk = _positive(raw.get("game_pk"), "history game_pk")
        if game_pk in seen:
            raise ValueError("prior history contains duplicate gamePk")
        seen.add(game_pk)
        if raw.get("verification_status") not in {
            "VERIFIED",
            "INELIGIBLE_UNPLAYED",
        }:
            raise ValueError("prior history requires verified source games")
        if run_id == SOURCE_RUN_ID:
            season = raw.get("season")
            if isinstance(season, bool) or not isinstance(season, int):
                raise ValueError("source game season differs")
            source_seasons.add(season)
        has_provenance = any(
            field in raw
            for field in ("raw_game_object_sha256", "normalized_anchor_sha256")
        )
        if run_id != SOURCE_RUN_ID:
            if has_provenance:
                raise ValueError("non-source history rejects source provenance")
        elif raw.get("verification_status") == "VERIFIED":
            anchor = _source_anchor(raw)
            if anchor["season"] == 2019:
                source_2019_anchors[game_pk] = anchor
        elif has_provenance:
            raise ValueError("unplayed source game has provenance")
        official = _day(raw.get("official_date"), "history official date")
        cutoff = _day(raw.get("feature_history_cutoff"), "history cutoff")
        if cutoff != official - timedelta(days=1):
            raise ValueError("every feature history cutoff must equal D-1")
        source_hash = raw.get("source_game_sha256")
        if not isinstance(source_hash, str) or not _SHA256.fullmatch(source_hash):
            raise ValueError("every projected row must bind a source game hash")
        projection = {
            "away_team_id": _positive(raw.get("away_team_id"), "away team"),
            "completed_innings": raw.get("completed_innings"),
            "doubleheader_order": _positive(
                raw.get("doubleheader_order"), "doubleheader order"
            ),
            "game_pk": game_pk,
            "history_cutoff": cutoff.isoformat(),
            "home_team_id": _positive(raw.get("home_team_id"), "home team"),
            "official_date": official.isoformat(),
            "scheduled_innings": raw.get("scheduled_innings"),
            "scheduled_start_utc": raw.get("scheduled_start_utc"),
            "source_game_sha256": source_hash,
            "terminal_status": raw.get("terminal_status"),
            "venue_id": _positive(raw.get("venue_id"), "venue id"),
        }
        for field in ("completed_innings", "scheduled_innings"):
            value = projection[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"history {field} must be a nonnegative integer or null")
        scheduled_start = projection["scheduled_start_utc"]
        if not isinstance(scheduled_start, str) or not scheduled_start.endswith("Z"):
            raise ValueError("history scheduled_start_utc must be canonical UTC")
        try:
            datetime.fromisoformat(scheduled_start[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError(
                "history scheduled_start_utc must be canonical UTC"
            ) from exc
        if not isinstance(projection["terminal_status"], str) or not projection[
            "terminal_status"
        ]:
            raise ValueError("history terminal_status must be nonempty text")
        projection["projection_sha256"] = canonical_sha256(projection)
        games.append(projection)
        source_hashes.append(source_hash)
    if len(source_hashes) != len(set(source_hashes)):
        raise ValueError("prior history contains duplicate source game hashes")
    games.sort(key=lambda row: (row["official_date"], row["game_pk"]))
    if run_id != SOURCE_RUN_ID:
        if duplicate_gamepk_disposition is not None:
            raise ValueError("non-source history rejects duplicate disposition")
        return {
            "games": games,
            "grain": "verified_game_projection",
            "schema_version": 1,
            "source_game_sha256s": sorted(source_hashes),
            "source_snapshot_sha256": source_snapshot_sha256,
        }
    if len(source_seasons) != 1:
        raise ValueError("source history requires one season")
    season = next(iter(source_seasons))
    if season != 2019:
        if duplicate_gamepk_disposition is not None:
            raise ValueError("source non-2019 history rejects duplicate disposition")
        return {
            "games": games,
            "grain": "verified_game_projection",
            "schema_version": 1,
            "source_game_sha256s": sorted(source_hashes),
            "source_snapshot_sha256": source_snapshot_sha256,
        }
    validate_task10_disposition_binding(duplicate_gamepk_disposition)
    expected_anchors = {
        game_pk: (raw_sha256, anchor_sha256, official_date)
        for raw_sha256, anchor_sha256, game_pk, official_date, _team_id in EXPECTED_NEUTRALIZATIONS
    }
    for game_pk, (raw_sha256, anchor_sha256, official_date) in expected_anchors.items():
        anchor = source_2019_anchors.get(game_pk)
        if anchor is None:
            raise ValueError("source history neutralization target is missing")
        if (
            anchor["raw_game_object_sha256"] != raw_sha256
            or anchor["official_date"] != official_date
            or canonical_sha256(anchor) != anchor_sha256
        ):
            raise ValueError("source history neutralization target differs")
    for raw_sha256, anchor_sha256, game_pk, official_date, team_id in EXPECTED_NEUTRALIZATIONS:
        anchor = source_2019_anchors[game_pk]
        if team_id not in {anchor["away_team_id"], anchor["home_team_id"]}:
            raise ValueError("source history neutralization team differs")
    input_identities = {
        (
            anchor["raw_game_object_sha256"],
            canonical_sha256(anchor),
            game_pk,
            anchor["official_date"],
            team_id,
        )
        for game_pk, anchor in source_2019_anchors.items()
        for team_id in (anchor["away_team_id"], anchor["home_team_id"])
    }
    if not set(EXPECTED_NEUTRALIZATIONS).issubset(input_identities):
        raise ValueError("source history neutralization targets differ")
    if not set(KNOWN_UNAPPROVED_NEIGHBORS).issubset(input_identities):
        raise ValueError("source history known neighbor differs")
    neutralizations = validate_schedule_neutralizations(
        [dict(record) for record in _expected_neutralization_records()]
    )
    emitted_identities = {
        (
            record["target_raw_game_object_sha256"],
            record["target_normalized_anchor_sha256"],
            record["game_pk"],
            record["official_date"],
            record["team_id"],
        )
        for record in neutralizations
    }
    if emitted_identities & set(KNOWN_UNAPPROVED_NEIGHBORS):
        raise ValueError("source history emitted an unapproved neighbor")
    return {
        "games": games,
        "grain": "verified_game_projection",
        "schedule_neutralizations": [dict(record) for record in neutralizations],
        "schema_version": 1,
        "source_game_sha256s": sorted(source_hashes),
        "source_snapshot_sha256": source_snapshot_sha256,
    }


__all__ = [
    "EXPECTED_NEUTRALIZATIONS",
    "KNOWN_UNAPPROVED_NEIGHBORS",
    "SOURCE_RUN_ID",
    "TASK10_DISPOSITION_BINDING",
    "TASK10_DISPOSITION_QUERY_ID",
    "TASK10_DISPOSITION_SIDECAR_PATH",
    "TASK10_DISPOSITION_SIDECAR_SHA256",
    "TASK10_DISPOSITION_SUCCESS_PATH",
    "TASK10_DISPOSITION_SUCCESS_SHA256",
    "recompute_prior_history_sidecar",
    "validate_task10_disposition_binding",
    "validate_schedule_neutralizations",
]
