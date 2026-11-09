"""Small hermetic sealed-ML fixtures for prospective observer tests."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mlb_backtest.live_top3_contracts import canonical_json_bytes


GAME_PK = 777001
AWAY_TEAM = "Away Club"
HOME_TEAM = "Home Club"
AWAY_TEAM_ID = 110
HOME_TEAM_ID = 120
AWAY_STARTER_ID = 501
HOME_STARTER_ID = 601
MODEL_CARD_SHA256 = "a" * 64


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _write_json(root: Path, relative: Path, value: object) -> tuple[Path, str, int]:
    contents = canonical_json_bytes(value)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return relative, _digest(contents), len(contents)


def _write_sidecar(root: Path, relative: Path, digest: str) -> None:
    path = root / relative.with_suffix(relative.suffix + ".sha256")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"{digest}\n".encode("ascii"))


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("start must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("start must be canonical UTC") from exc
    if parsed.tzinfo is None:
        raise ValueError("start must be canonical UTC")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _participant_payload(observed: Mapping[str, object] | None) -> dict[str, object]:
    fixture = Path(__file__).parent / "fixtures" / "live_top3" / "boxscore_complete.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    teams = payload["teams"]
    teams["away"]["team"] = {"id": AWAY_TEAM_ID}
    teams["home"]["team"] = {"id": HOME_TEAM_ID}

    overrides = dict(observed or {})
    for side in ("away", "home"):
        nested = overrides.get(side)
        if isinstance(nested, Mapping):
            side_payload = teams[side]
            if "pitchers" in nested:
                side_payload["pitchers"] = copy.deepcopy(nested["pitchers"])
            players = nested.get("players", nested.get("lineup"))
            if players is not None:
                side_payload["players"] = copy.deepcopy(players)
        for field in ("pitchers", "players"):
            flat = overrides.get(f"{side}_{field}")
            if flat is not None:
                teams[side][field] = copy.deepcopy(flat)
        flat_lineup = overrides.get(f"{side}_lineup")
        if flat_lineup is not None:
            teams[side]["players"] = copy.deepcopy(flat_lineup)
    return payload


def _schedule_payload(target_date: str, start: str) -> dict[str, object]:
    fixture = Path(__file__).parent / "fixtures" / "live_top3" / "schedule_complete.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    entry = payload["dates"][0]
    entry["date"] = target_date
    game = entry["games"][0]
    game["gamePk"] = GAME_PK
    game["officialDate"] = target_date
    game["gameDate"] = start
    game["status"] = {"detailedState": "Pre-Game"}
    game["teams"]["away"]["team"] = {"id": AWAY_TEAM_ID, "name": AWAY_TEAM}
    game["teams"]["home"]["team"] = {"id": HOME_TEAM_ID, "name": HOME_TEAM}
    game["teams"]["away"]["probablePitcher"] = {
        "id": AWAY_STARTER_ID,
        "fullName": "Away Pitcher",
    }
    game["teams"]["home"]["probablePitcher"] = {
        "id": HOME_STARTER_ID,
        "fullName": "Home Pitcher",
    }
    game["teams"]["away"].pop("score", None)
    game["teams"]["away"].pop("isWinner", None)
    game["teams"]["home"].pop("score", None)
    game["teams"]["home"].pop("isWinner", None)
    game.pop("linescore", None)
    return payload


def _binding(
    *,
    role: str,
    game_pks: list[int],
    observation_id: str,
    url: str,
    payload_relative: Path,
    payload_sha256: str,
    byte_length: int,
    receipt_relative: Path,
    receipt_sha256: str,
    manifest_sha256: str,
    captured_at: str,
) -> dict[str, object]:
    binding: dict[str, object] = {
        "source": "stats_api",
        "binding_role": role,
        "game_pks": game_pks,
        "observation_id": observation_id,
        "query_identity_json": canonical_json_bytes(
            {"observation_id": observation_id, "url": url}
        ).decode("utf-8").removesuffix("\n"),
        "requested_url": url,
        "final_url": url,
        "response_completed_at_utc": captured_at,
        "durable_at_utc": captured_at,
        "http_status": 200,
        "content_type": "application/json;charset=UTF-8",
        "etag": None,
        "last_modified": None,
        "payload_sha256": payload_sha256,
        "byte_length": byte_length,
        "payload_relative": payload_relative.as_posix(),
        "manifest_sha256": manifest_sha256,
        "receipt_relative": receipt_relative.as_posix(),
        "receipt_sha256": receipt_sha256,
    }
    binding["binding_sha256"] = _digest(canonical_json_bytes(binding))
    return binding


def _report(
    *,
    target_date: str,
    history_cutoff: str,
    executed_at_utc: str,
    executed_at_eastern: str,
    bindings: list[dict[str, object]],
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": 1,
        "model_label": "v5.1-EXPERIMENTAL-LIVE",
        "target_date": target_date,
        "command_inputs": {
            "date": target_date,
            "model_card": "audit/v5_1_bt/model_card.json",
        },
        "executed_at_eastern": executed_at_eastern,
        "executed_at_utc": executed_at_utc,
        "history_cutoff_date": history_cutoff,
        "cache_authority": {
            "namespace": "experimental_daily/history_cache",
            "cutoff_date": history_cutoff,
            "catalog_sha256": "b" * 64,
        },
        "model_card_sha256": MODEL_CARD_SHA256,
        "source_bindings": bindings,
        "verified_top3": [],
        "preliminary_top3": [],
        "unscored_games": [],
        "excluded_games": [],
        "warnings": [],
        "content_sha256": "",
    }
    report["content_sha256"] = _digest(
        canonical_json_bytes({key: value for key, value in report.items() if key != "content_sha256"})
    )
    return report


def _trace(*, target_date: str, primary_relative: Path, primary: Mapping[str, object], p_home: float, completeness: float, start: str) -> dict[str, object]:
    side = "home" if p_home > 0.5 else "away" if p_home < 0.5 else "none"
    quality = {
        "starter": completeness,
        "offense": completeness,
        "bullpen": completeness,
        "environment": completeness,
        "defense": completeness,
        "schedule": completeness,
    }
    zero = {key: 0.0 for key in quality}
    raw_features = {"eligible": True, "game_pk": GAME_PK, "official_date": target_date}
    row: dict[str, object] = {
        "game_pk": GAME_PK,
        "target_date": target_date,
        "away_team": AWAY_TEAM,
        "home_team": HOME_TEAM,
        "scheduled_start_utc": start,
        "raw_features": raw_features,
        "feature_sha256": _digest(canonical_json_bytes(raw_features)),
        "model_card_sha256": MODEL_CARD_SHA256,
        "away_scores": zero,
        "home_scores": zero,
        "qualities": quality,
        "contributions": zero,
        "total_edge": 0.0,
        "p_home": p_home,
        "predicted_side": side,
        "pick_probability": max(p_home, 1.0 - p_home),
        "primary_rank": None,
        "disposition": "SCORED_NOT_TOP3",
    }
    trace: dict[str, object] = {
        "schema_version": 1,
        "research_status": "PRELIMINARY_RESEARCH_ONLY",
        "not_a_betting_recommendation": True,
        "primary_report_relative": primary_relative.as_posix(),
        "primary_report_content_sha256": str(primary["content_sha256"]),
        "model_card_sha256": MODEL_CARD_SHA256,
        "games": [row],
        "content_sha256": "",
    }
    trace["content_sha256"] = _digest(
        canonical_json_bytes({key: value for key, value in trace.items() if key != "content_sha256"})
    )
    return trace


def sealed_ml_fixture(
    root: Path,
    *,
    p_home: float = 0.7,
    completeness: float = 0.4,
    start: str = "2026-09-03T20:00:00Z",
    observed: Mapping[str, object] | None = None,
) -> Path:
    """Write one scored sealed ML trace and return its root-relative path.

    ``observed`` accepts nested ``away``/``home`` ``pitchers`` and ``players``
    values, or equivalent ``away_pitchers``/``home_pitchers`` and
    ``away_players``/``home_players`` keys.
    """
    if isinstance(p_home, bool) or not 0.0 <= float(p_home) <= 1.0:
        raise ValueError("p_home must be in [0, 1]")
    if isinstance(completeness, bool) or not 0.0 <= float(completeness) <= 1.0:
        raise ValueError("completeness must be in [0, 1]")
    root = Path(root)
    start_dt = _utc(start)
    start = _utc_text(start_dt)
    target_date = start[:10]
    history_cutoff = _utc_text(start_dt - timedelta(days=1))[:10]
    captured_at = _utc_text(start_dt - timedelta(minutes=30))
    schedule_url = (
        "https://statsapi.mlb.com/api/v1/schedule?"
        f"sportId=1&gameType=R&startDate={target_date}&endDate={target_date}"
    )
    participant_url = f"https://statsapi.mlb.com/api/v1/game/{GAME_PK}/boxscore"
    schedule_relative = Path(
        "data/experimental_daily/live_observations/mlb_live_schedule/777001.schedule.raw"
    )
    participant_relative = Path(
        "data/experimental_daily/live_observations/mlb_live_boxscore/777001.participants.raw"
    )
    schedule_receipt_relative = Path("audit/experimental_daily/live_receipts/777001.schedule.json")
    participant_receipt_relative = Path("audit/experimental_daily/live_receipts/777001.participants.json")
    schedule_manifest_relative = Path("audit/experimental_daily/live_receipts/777001.schedule.manifest.json")
    participant_manifest_relative = Path("audit/experimental_daily/live_receipts/777001.participants.manifest.json")
    schedule_relative, schedule_sha, schedule_size = _write_json(
        root, schedule_relative, _schedule_payload(target_date, start)
    )
    participant_relative, participant_sha, participant_size = _write_json(
        root, participant_relative, _participant_payload(observed)
    )
    _write_sidecar(root, schedule_relative, schedule_sha)
    _write_sidecar(root, participant_relative, participant_sha)
    _, schedule_manifest_sha, _ = _write_json(
        root, schedule_manifest_relative, {"payload_sha256": schedule_sha}
    )
    _, participant_manifest_sha, _ = _write_json(
        root, participant_manifest_relative, {"payload_sha256": participant_sha}
    )
    _, schedule_receipt_sha, _ = _write_json(
        root,
        schedule_receipt_relative,
        {
            "payload_relative": schedule_relative.as_posix(),
            "payload_sha256": schedule_sha,
            "manifest_relative": schedule_manifest_relative.as_posix(),
            "manifest_sha256": schedule_manifest_sha,
        },
    )
    _, participant_receipt_sha, _ = _write_json(
        root,
        participant_receipt_relative,
        {
            "payload_relative": participant_relative.as_posix(),
            "payload_sha256": participant_sha,
            "manifest_relative": participant_manifest_relative.as_posix(),
            "manifest_sha256": participant_manifest_sha,
        },
    )
    primary_relative = Path(f"reports/experimental_daily/{target_date}/sealed-primary.json")
    executed_dt = start_dt - timedelta(minutes=20)
    executed_at_utc = _utc_text(executed_dt)
    executed_at_eastern = executed_dt.astimezone(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    bindings = [
        _binding(
            role="LIVE_SCHEDULE", game_pks=[GAME_PK], observation_id="schedule-777001",
            url=schedule_url, payload_relative=schedule_relative, payload_sha256=schedule_sha,
            byte_length=schedule_size, receipt_relative=schedule_receipt_relative,
            receipt_sha256=schedule_receipt_sha, manifest_sha256=schedule_manifest_sha,
            captured_at=captured_at,
        ),
        _binding(
            role="LIVE_PARTICIPANTS", game_pks=[GAME_PK], observation_id="participants-777001",
            url=participant_url, payload_relative=participant_relative, payload_sha256=participant_sha,
            byte_length=participant_size, receipt_relative=participant_receipt_relative,
            receipt_sha256=participant_receipt_sha, manifest_sha256=participant_manifest_sha,
            captured_at=captured_at,
        ),
    ]
    primary = _report(
        target_date=target_date, history_cutoff=history_cutoff,
        executed_at_utc=executed_at_utc, executed_at_eastern=executed_at_eastern,
        bindings=bindings,
    )
    _write_json(root, primary_relative, primary)
    _write_sidecar(root, primary_relative, _digest(canonical_json_bytes(primary)))
    trace_relative = Path(f"reports/experimental_daily/{target_date}/sealed.trace.json")
    trace = _trace(
        target_date=target_date, primary_relative=primary_relative,
        primary=primary, p_home=float(p_home), completeness=float(completeness), start=start,
    )
    _write_json(root, trace_relative, trace)
    _write_sidecar(root, trace_relative, _digest(canonical_json_bytes(trace)))
    return trace_relative


__all__ = ["sealed_ml_fixture"]
