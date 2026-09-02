"""Normalize admitted sportsbook captures onto an immutable MLB game slate."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .market_play_contracts import (
    SUPPORTED_BOOK_IDS,
    TotalQuote,
    MlQuote,
    canonical_json_bytes,
    canonical_utc,
    content_digest,
    sidecar_bytes,
    validate_relative_path,
)
from .multibook_market_capture import LoadedCaptureOutcome, load_capture_outcome, validate_capture_outcome
from .secure_fs import (
    RegularFileSnapshot,
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


SCHEDULE_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_SCHEDULE_V1"
BOARD_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V1"
BOARD_PREFIX = PurePosixPath("data/experimental_multibook_market/boards")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TEAM_RE = re.compile(r"[^A-Z0-9]+")

# These aliases are deliberately small and deterministic.  Unknown names are
# normalized only by case and punctuation; the normalizer never guesses from
# a team name alone when the resulting identity is ambiguous.
_TEAM_ALIASES = {
    "NYY": "NEWYORKYANKEES",
    "NYM": "NEWYORKMETS",
    "SD": "SANDIEGOPADRES",
    "CIN": "CINCINNATIREDS",
    "SF": "SANFRANCISCOGIANTS",
    "KC": "KANSASCITYROYALS",
    "MIN": "MINNESOTATWINS",
    "CLE": "CLEVELANDGUARDIANS",
    "LAA": "LOSANGELESANGELS",
    "CHC": "CHICAGOCUBS",
    "ARI": "ARIZONADIAMONDBACKS",
    "PIT": "PITTSBURGHPIRATES",
    "SEA": "SEATTLEMARINERS",
    "PHI": "PHILADELPHIAPHILLIES",
}


@dataclass(frozen=True, slots=True)
class ScheduleAuthority:
    target_date: str
    games: tuple[dict[str, object], ...]
    receipt_relative: Path
    receipt_snapshot: RegularFileSnapshot
    receipt_sidecar_snapshot: RegularFileSnapshot

    @property
    def by_game(self) -> Mapping[int, dict[str, object]]:
        return MappingProxyType({int(game["game_pk"]): game for game in self.games})


@dataclass(frozen=True, slots=True)
class MarketBoardGame:
    game_pk: int
    away_team: str
    home_team: str
    scheduled_start_utc: str
    moneylines: tuple[MlQuote, ...]
    totals: tuple[TotalQuote, ...]


@dataclass(frozen=True, slots=True)
class MarketBoardAuthority:
    target_date: str
    games: tuple[MarketBoardGame, ...]
    board_relative: Path
    board_snapshot: RegularFileSnapshot
    board_sidecar_snapshot: RegularFileSnapshot

    @property
    def by_game(self) -> Mapping[int, MarketBoardGame]:
        return MappingProxyType({game.game_pk: game for game in self.games})


@dataclass(frozen=True, slots=True)
class BuiltMarketBoard:
    board: Mapping[str, object]
    board_bytes: bytes
    sidecar_bytes: bytes
    input_snapshots: tuple[RegularFileSnapshot, ...]


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise ValueError(
            f"{label} keys differ; missing={sorted(keys - actual)} "
            f"unknown={sorted(actual - keys)}"
        )
    return dict(value)


def _read_json(root: TrustedRoot, relative: Path, label: str, max_bytes: int) -> tuple[RegularFileSnapshot, RegularFileSnapshot, dict[str, object]]:
    snapshot = read_regular_snapshot(root, relative, label, max_bytes=max_bytes)
    assert snapshot is not None
    sidecar = read_regular_snapshot(root, relative.with_suffix(".sha256"), f"{label} sidecar", max_bytes=65)
    assert sidecar is not None
    if sidecar.contents != sidecar_bytes(snapshot.contents):
        raise ValueError(f"{label} sidecar differs")
    try:
        payload = json.loads(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return snapshot, sidecar, payload


def _team_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("team name must be non-empty")
    compact = _TEAM_RE.sub("", value.upper())
    if not compact:
        raise ValueError("team name is empty after normalization")
    return _TEAM_ALIASES.get(compact, compact)


def _same_teams(event: Mapping[str, object], game: Mapping[str, object]) -> bool:
    return (
        _team_key(event["away_team"]) == _team_key(game["away_team"])
        and _team_key(event["home_team"]) == _team_key(game["home_team"])
    )


def _same_start(event: Mapping[str, object], game: Mapping[str, object]) -> bool:
    return event["scheduled_start_utc"] == game["scheduled_start_utc"]


def _match_event(event: Mapping[str, object], games: Sequence[Mapping[str, object]]) -> int:
    matches = [game for game in games if _same_teams(event, game) and _same_start(event, game)]
    if len(matches) != 1:
        raise ValueError("market event does not map uniquely to the official slate")
    return int(matches[0]["game_pk"])


def _validate_schedule(value: object) -> tuple[str, tuple[dict[str, object], ...]]:
    item = _exact(value, {"schema", "content_sha256", "target_date", "games"}, "schedule")
    if item["schema"] != SCHEDULE_SCHEMA:
        raise ValueError("schedule schema differs")
    if not isinstance(item["target_date"], str) or _DATE_RE.fullmatch(item["target_date"]) is None:
        raise ValueError("schedule target_date differs")
    games_value = item["games"]
    if not isinstance(games_value, list) or not games_value:
        raise ValueError("schedule games must be a non-empty list")
    seen: set[int] = set()
    games: list[dict[str, object]] = []
    for index, raw in enumerate(games_value):
        game = _exact(raw, {"game_pk", "away_team", "home_team", "scheduled_start_utc"}, f"schedule.games[{index}]")
        game_pk = game["game_pk"]
        if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0 or game_pk in seen:
            raise ValueError("schedule game_pk must be unique positive integer")
        seen.add(game_pk)
        _team_key(game["away_team"])
        _team_key(game["home_team"])
        if _team_key(game["away_team"]) == _team_key(game["home_team"]):
            raise ValueError("schedule away/home teams must differ")
        canonical_utc(game["scheduled_start_utc"])
        games.append(game)
    games.sort(key=lambda value: int(value["game_pk"]))
    return item["target_date"], tuple(games)


def load_schedule_authority(root: Path, schedule_receipt: Path) -> ScheduleAuthority:
    relative = validate_relative_path(schedule_receipt)
    with open_trusted_root(root) as trusted:
        snapshot, sidecar, payload = _read_json(trusted, relative, "schedule receipt", 2 * 1024 * 1024)
        without = dict(payload)
        digest = without.get("content_sha256")
        without["content_sha256"] = ""
        if digest != content_digest(without):
            raise ValueError("schedule content_sha256 differs")
        target_date, games = _validate_schedule(payload)
        return ScheduleAuthority(target_date, games, relative, snapshot, sidecar)


def _quote_board_game(game: Mapping[str, object], outcome: LoadedCaptureOutcome, *, book_id: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if outcome.status != "AVAILABLE":
        return [], []
    raw_relative = str(outcome.payload["raw_capture_relative"])
    raw_digest = str(outcome.payload["raw_capture_sha256"])
    moneylines: list[dict[str, object]] = []
    totals: list[dict[str, object]] = []
    raw_payload = json.loads(outcome.raw_snapshot.contents.decode("utf-8")) if outcome.raw_snapshot else {}
    for event in raw_payload["events"]:
        if not (_same_teams(event, game) and _same_start(event, game)):
            continue
        event_id = event["book_event_id"]
        moneyline = event["moneyline"]
        moneylines.append(
            {
                "game_pk": game["game_pk"],
                "book_id": book_id,
                "event_id": event_id,
                "captured_at_utc": outcome.payload["captured_at_utc"],
                "away_american_odds": moneyline["away_american_odds"],
                "home_american_odds": moneyline["home_american_odds"],
                "raw_capture_relative": raw_relative,
                "raw_capture_sha256": raw_digest,
            }
        )
        for total in event["totals"]:
            totals.append(
                {
                    "game_pk": game["game_pk"],
                    "book_id": book_id,
                    "event_id": event_id,
                    "captured_at_utc": outcome.payload["captured_at_utc"],
                    "line_half_runs": total["line_half_runs"],
                    "over_american_odds": total["over_american_odds"],
                    "under_american_odds": total["under_american_odds"],
                    "raw_capture_relative": raw_relative,
                    "raw_capture_sha256": raw_digest,
                }
            )
    return moneylines, totals


def normalize_market_board(root: Path, schedule_receipt: Path, capture_outcomes: Sequence[Path]) -> BuiltMarketBoard:
    schedule_relative = validate_relative_path(schedule_receipt)
    if len(capture_outcomes) != len(SUPPORTED_BOOK_IDS):
        raise ValueError("exactly five capture outcomes are required")
    outcome_relatives = tuple(validate_relative_path(path) for path in capture_outcomes)
    with open_trusted_root(root) as trusted:
        schedule_snapshot, schedule_sidecar, schedule_payload = _read_json(trusted, schedule_relative, "schedule receipt", 2 * 1024 * 1024)
        schedule_digest = schedule_payload.get("content_sha256")
        schedule_without = dict(schedule_payload)
        schedule_without["content_sha256"] = ""
        if schedule_digest != content_digest(schedule_without):
            raise ValueError("schedule content_sha256 differs")
        target_date, games = _validate_schedule(schedule_payload)
        loaded = [load_capture_outcome(trusted, relative) for relative in outcome_relatives]
        books = [outcome.payload["source_book"] for outcome in loaded]
        if set(books) != set(SUPPORTED_BOOK_IDS) or len(set(books)) != len(SUPPORTED_BOOK_IDS):
            raise ValueError("capture outcomes must contain one of each supported book")
        for outcome in loaded:
            if outcome.status == "AVAILABLE" and outcome.raw_snapshot is not None:
                raw_payload = json.loads(outcome.raw_snapshot.contents.decode("utf-8"))
                mapped_games: set[int] = set()
                for event in raw_payload["events"]:
                    mapped_game = _match_event(event, games)
                    if mapped_game in mapped_games:
                        raise ValueError("duplicate book event mapped to one game")
                    mapped_games.add(mapped_game)
        board_games: list[dict[str, object]] = []
        game_by_pk = {int(game["game_pk"]): game for game in games}
        for game in games:
            moneylines: list[dict[str, object]] = []
            totals: list[dict[str, object]] = []
            for outcome in loaded:
                ml_rows, total_rows = _quote_board_game(game, outcome, book_id=str(outcome.payload["source_book"]))
                moneylines.extend(ml_rows)
                totals.extend(total_rows)
            moneylines.sort(key=lambda row: (SUPPORTED_BOOK_IDS.index(str(row["book_id"])), str(row["event_id"])))
            totals.sort(key=lambda row: (int(row["line_half_runs"]), SUPPORTED_BOOK_IDS.index(str(row["book_id"])), str(row["event_id"])))
            board_games.append({**game, "moneylines": moneylines, "totals": totals})
        del game_by_pk
        board: dict[str, object] = {
            "schema": BOARD_SCHEMA,
            "content_sha256": "",
            "target_date": target_date,
            "schedule_receipt_relative": schedule_relative.as_posix(),
            "schedule_receipt_sha256": hashlib.sha256(schedule_snapshot.contents).hexdigest(),
            "capture_outcomes": [outcome.payload for outcome in sorted(loaded, key=lambda item: SUPPORTED_BOOK_IDS.index(str(item.payload["source_book"])))],
            "games": board_games,
        }
        board["content_sha256"] = content_digest(board)
        board_bytes = canonical_json_bytes(board)
        snapshots: list[RegularFileSnapshot] = [schedule_snapshot, schedule_sidecar]
        for outcome in loaded:
            snapshots.extend((outcome.snapshot, outcome.sidecar_snapshot))
            if outcome.raw_snapshot is not None and outcome.raw_sidecar_snapshot is not None:
                snapshots.extend((outcome.raw_snapshot, outcome.raw_sidecar_snapshot))
        return BuiltMarketBoard(board, board_bytes, sidecar_bytes(board_bytes), tuple(snapshots))


def _write_or_reuse(trusted: TrustedRoot, relative: Path, contents: bytes, label: str) -> RegularFileSnapshot:
    existing = read_regular_snapshot(trusted, relative, label, max_bytes=max(len(contents), 65), missing_ok=True)
    if existing is not None:
        if existing.contents != contents:
            raise ValueError(f"conflicting append-only {label}")
        return existing
    return write_exclusive(trusted, relative, contents, label)


def persist_market_board(root: Path, built: BuiltMarketBoard) -> tuple[Path, Path]:
    if not isinstance(built, BuiltMarketBoard):
        raise TypeError("built board differs")
    board = dict(built.board)
    if board.get("content_sha256") != content_digest({**board, "content_sha256": ""}):
        raise ValueError("built board digest differs")
    expected_bytes = canonical_json_bytes(board)
    if expected_bytes != built.board_bytes or sidecar_bytes(expected_bytes) != built.sidecar_bytes:
        raise ValueError("built board bytes differ")
    relative = Path(BOARD_PREFIX / f"{board['content_sha256']}.json")
    with open_trusted_root(root) as trusted:
        for snapshot in built.input_snapshots:
            rebind_regular_snapshot(trusted, snapshot, "market board input")
        _write_or_reuse(trusted, relative, built.board_bytes, "market board")
        _write_or_reuse(trusted, relative.with_suffix(".sha256"), built.sidecar_bytes, "market board sidecar")
    return relative, relative.with_suffix(".sha256")


def load_market_board(root: Path, board_path: Path, schedule: ScheduleAuthority) -> MarketBoardAuthority:
    relative = validate_relative_path(board_path)
    with open_trusted_root(root) as trusted:
        snapshot, sidecar, payload = _read_json(trusted, relative, "market board", 8 * 1024 * 1024)
        if payload.get("schema") != BOARD_SCHEMA:
            raise ValueError("market board schema differs")
        digest = payload.get("content_sha256")
        without = dict(payload)
        without["content_sha256"] = ""
        if digest != content_digest(without) or relative.stem != digest:
            raise ValueError("market board content digest differs")
        if payload.get("target_date") != schedule.target_date or payload.get("schedule_receipt_relative") != schedule.receipt_relative.as_posix():
            raise ValueError("market board schedule binding differs")
        outcomes_value = payload.get("capture_outcomes")
        if not isinstance(outcomes_value, list) or len(outcomes_value) != len(SUPPORTED_BOOK_IDS):
            raise ValueError("market board capture outcome population differs")
        outcomes = [validate_capture_outcome(value) for value in outcomes_value]
        outcome_books = [str(value["source_book"]) for value in outcomes]
        if set(outcome_books) != set(SUPPORTED_BOOK_IDS) or len(set(outcome_books)) != len(SUPPORTED_BOOK_IDS):
            raise ValueError("market board capture outcomes differ")
        outcome_paths = {
            str(value["raw_capture_relative"]): str(value["raw_capture_sha256"])
            for value in outcomes
            if value["status"] == "AVAILABLE"
        }
        games_value = payload.get("games")
        if not isinstance(games_value, list) or len(games_value) != len(schedule.games):
            raise ValueError("market board game population differs")
        games: list[MarketBoardGame] = []
        for raw_game, schedule_game in zip(games_value, schedule.games, strict=True):
            game = _exact(raw_game, {"game_pk", "away_team", "home_team", "scheduled_start_utc", "moneylines", "totals"}, "market board game")
            if game["game_pk"] != schedule_game["game_pk"] or game["away_team"] != schedule_game["away_team"] or game["home_team"] != schedule_game["home_team"] or game["scheduled_start_utc"] != schedule_game["scheduled_start_utc"]:
                raise ValueError("market board game differs from schedule")
            if not isinstance(game["moneylines"], list) or not isinstance(game["totals"], list):
                raise ValueError("market board quote collections must be lists")
            ml_quotes_list: list[MlQuote] = []
            seen_ml_books: set[str] = set()
            for raw_quote in game["moneylines"]:
                quote = _exact(raw_quote, {"game_pk", "book_id", "event_id", "captured_at_utc", "away_american_odds", "home_american_odds", "raw_capture_relative", "raw_capture_sha256"}, "market board moneyline")
                if quote["book_id"] in seen_ml_books:
                    raise ValueError("duplicate moneyline book for game")
                seen_ml_books.add(str(quote["book_id"]))
                if quote["game_pk"] != game["game_pk"] or outcome_paths.get(str(quote["raw_capture_relative"])) != quote["raw_capture_sha256"]:
                    raise ValueError("moneyline raw capture binding is absent")
                ml_quotes_list.append(MlQuote(int(quote["game_pk"]), str(quote["book_id"]), str(quote["event_id"]), str(quote["captured_at_utc"]), int(quote["away_american_odds"]), int(quote["home_american_odds"])))
            total_quotes_list: list[TotalQuote] = []
            seen_total_keys: set[tuple[str, int]] = set()
            for raw_quote in game["totals"]:
                quote = _exact(raw_quote, {"game_pk", "book_id", "event_id", "captured_at_utc", "line_half_runs", "over_american_odds", "under_american_odds", "raw_capture_relative", "raw_capture_sha256"}, "market board total")
                key = (str(quote["book_id"]), int(quote["line_half_runs"]))
                if key in seen_total_keys:
                    raise ValueError("duplicate total book/line for game")
                seen_total_keys.add(key)
                if quote["game_pk"] != game["game_pk"] or outcome_paths.get(str(quote["raw_capture_relative"])) != quote["raw_capture_sha256"]:
                    raise ValueError("total raw capture binding is absent")
                total_quotes_list.append(TotalQuote(int(quote["game_pk"]), str(quote["book_id"]), str(quote["event_id"]), str(quote["captured_at_utc"]), int(quote["line_half_runs"]), int(quote["over_american_odds"]), int(quote["under_american_odds"])))
            ml_quotes = tuple(ml_quotes_list)
            total_quotes = tuple(total_quotes_list)
            games.append(MarketBoardGame(int(game["game_pk"]), str(game["away_team"]), str(game["home_team"]), str(game["scheduled_start_utc"]), ml_quotes, total_quotes))
        rebind_regular_snapshot(trusted, schedule.receipt_snapshot, "schedule receipt")
        rebind_regular_snapshot(trusted, schedule.receipt_sidecar_snapshot, "schedule receipt sidecar")
        return MarketBoardAuthority(str(payload["target_date"]), tuple(games), relative, snapshot, sidecar)
