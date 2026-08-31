"""Run and publish the research-only experimental live Daily Top 3 report."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from .live_history import build_live_history, load_live_career_bf
from .live_model_card import load_frozen_live_card_bytes
from .live_sources import (
    fetch_live_http,
    normalize_live_root_path,
    observe_final_game_schedule,
    observe_live_slate,
)
from .live_top3_contracts import (
    ExecutionTimes,
    IntegrityError,
    canonical_json_bytes,
    validate_run_payload,
    validate_target_date,
)
from .live_top3_service import build_live_top3_run
from .secure_fs import (
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


_EASTERN = ZoneInfo("America/New_York")
_REPORT_ROOT = Path("reports/experimental_daily")


def utc_now() -> datetime:
    """Return the current aware UTC instant; a small seam for deterministic CLI tests."""
    return datetime.now(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m mlb_backtest.daily_top3",
        description=__doc__,
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--date", dest="date", type=_date_argument, default=None)
    parser.add_argument(
        "--model-card", type=Path, default=Path("audit/v5_1_bt/model_card.json")
    )
    parser.add_argument("--json-only", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command arguments, choosing today's calendar date in US Eastern time."""
    args = _parser().parse_args(argv)
    if args.date is None:
        instant = utc_now()
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise ValueError("UTC clock must return an aware datetime")
        args.date = instant.astimezone(_EASTERN).date().isoformat()
    args.date = _date_argument(args.date)
    return args


def _date_argument(value: str) -> str:
    try:
        return validate_target_date(value)
    except (IntegrityError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO calendar date"
        ) from exc


def _root_and_card_path(root: Path, model_card_path: Path) -> tuple[Path, Path, str]:
    """Keep the card command input relative to the run root as required by the contract."""
    root_path = normalize_live_root_path(Path(root))
    supplied = Path(model_card_path)
    if (
        not supplied.parts
        or any(part in {"", ".", ".."} for part in supplied.parts)
        or "\\" in os.fspath(supplied)
    ):
        raise IntegrityError("model card path differs")
    if supplied.is_absolute():
        supplied = normalize_live_root_path(supplied)
        try:
            relative = supplied.relative_to(root_path)
        except ValueError as exc:
            raise IntegrityError("model card must be below --root") from exc
    else:
        relative = supplied
    card_path = root_path / relative
    return root_path, card_path, relative.as_posix()


def _load_bound_model_card(
    trusted: TrustedRoot, relative: str
):
    """Parse only bytes authenticated through the held root capability."""
    snapshot = read_regular_snapshot(
        trusted,
        Path(relative),
        "frozen live model card",
        max_bytes=16 * 1024 * 1024,
    )
    if snapshot is None:
        raise IntegrityError("frozen live model card differs")
    _model_card_snapshot_test_hook("after_snapshot", Path(relative))
    card = load_frozen_live_card_bytes(snapshot.contents)
    rebind_regular_snapshot(
        trusted,
        snapshot,
        "frozen live model card",
        max_bytes=16 * 1024 * 1024,
    )
    return card


def _model_card_snapshot_test_hook(stage: str, relative: Path) -> None:
    """No-op seam for deterministic model-card replacement tests."""


def _bound_call(trusted: TrustedRoot, function, /, *args, **kwargs):
    trusted.assert_bound()
    try:
        return function(*args, **kwargs)
    finally:
        trusted.assert_bound()


def _observed_pitcher_ids(slate: object) -> tuple[int, ...]:
    participants = getattr(slate, "participants", None)
    if not isinstance(participants, Mapping):
        raise IntegrityError("live slate participants differ")
    pitcher_ids: set[int] = set()
    for participant in participants.values():
        for field in ("away_pitcher_list_id", "home_pitcher_list_id"):
            pitcher_id = getattr(participant, field, None)
            if type(pitcher_id) is int and pitcher_id > 0:
                pitcher_ids.add(pitcher_id)
    return tuple(sorted(pitcher_ids))


def run_daily_top3(
    root: Path,
    target_date: str,
    model_card_path: Path,
    *,
    fetcher=fetch_live_http,
    utc_clock=utc_now,
) -> tuple[dict[str, object], Path, Path]:
    """Capture, score, and validate one fresh research-only live slate."""
    target_date = validate_target_date(target_date)
    root_path, _, card_relative = _root_and_card_path(root, model_card_path)
    execution_times = ExecutionTimes.capture(utc_clock)
    with open_trusted_root(root_path) as trusted:
        card = _bound_call(
            trusted, _load_bound_model_card, trusted, card_relative
        )
        history = _bound_call(
            trusted,
            build_live_history,
            root_path,
            target_date,
            card,
            fetcher=fetcher,
            expected_root_binding=trusted.binding,
        )
        slate = _bound_call(
            trusted,
            observe_live_slate,
            root_path,
            target_date,
            fetcher=fetcher,
            expected_root_binding=trusted.binding,
        )

        # Preserve the exact immutable live-slate mappings for final reconciliation.
        saved_games = MappingProxyType(
            {int(game["game_pk"]): game for game in slate.games}
        )
        if len(saved_games) != len(slate.games):
            raise IntegrityError("live slate game identities differ")

        career_bf_by_pitcher, career_bf_bindings = _bound_call(
            trusted,
            load_live_career_bf,
            root_path,
            target_date,
            _observed_pitcher_ids(slate),
            fetcher=fetcher,
            expected_root_binding=trusted.binding,
        )

        def observe_final(game_pk: int, requested_date: str):
            if requested_date != target_date or game_pk not in saved_games:
                raise IntegrityError(
                    "final observation request differs from saved slate"
                )
            return _bound_call(
                trusted,
                observe_final_game_schedule,
                root_path,
                requested_date,
                saved_games[game_pk],
                fetcher=fetcher,
                expected_root_binding=trusted.binding,
            )

        payload = _bound_call(
            trusted,
            build_live_top3_run,
            slate=slate,
            final_schedule_observer=observe_final,
            history=history,
            card=card,
            career_bf_by_pitcher=career_bf_by_pitcher,
            career_bf_bindings=career_bf_bindings,
            command_inputs={"date": target_date, "model_card": card_relative},
            execution_times=execution_times,
        )
        validated = validate_run_payload(payload)
        json_path, markdown_path = _bound_call(
            trusted,
            _persist_bound_live_top3_report,
            root_path,
            trusted,
            validated,
        )
        _bound_call(
            trusted,
            _persist_bound_live_pregame_readiness_report,
            root_path,
            trusted,
            slate,
            validated,
        )
        trusted.assert_bound()
        return validated, json_path, markdown_path


def _escape_table(value: object) -> str:
    escaped = (
        html.escape(str(value), quote=True)
        .replace("\\", "\\\\")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
        .replace("|", "\\|")
    )
    for character in "[]()!*_~`":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    return "\n".join(
        (
            "| " + " | ".join(_escape_table(value) for value in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *(
                "| " + " | ".join(_escape_table(value) for value in row) + " |"
                for row in rows
            ),
        )
    )


def _reason_text(row: Mapping[str, object]) -> str:
    return "; ".join(str(value) for value in row["reason_codes"]) or "none"


def _schedule_quality_flags_text(row: Mapping[str, object]) -> str:
    flags = row.get("schedule_quality_flags")
    if flags is None:
        return "not recorded"
    return "; ".join(
        f"{side}: {', '.join(flags[side]) or 'none'}"
        for side in ("away", "home")
    )


def _ranked_table(rows: Sequence[Mapping[str, object]]) -> str:
    return _markdown_table(
        ("Rank", "Game", "Pick", "Probability", "Completeness", "Start (UTC)", "Reasons", "Schedule quality flags"),
        [
            (
                row["rank"],
                f'{row["away_team"]} at {row["home_team"]}',
                row["predicted_winner"],
                row["pick_probability_display"],
                f'{float(row["data_completeness"]):.1%}',
                row["scheduled_start_utc"],
                _reason_text(row),
                _schedule_quality_flags_text(row),
            )
            for row in rows
        ],
    )


def _unranked_table(rows: Sequence[Mapping[str, object]]) -> str:
    # Do not imply a score or ordering for unavailable/excluded games.
    return _markdown_table(
        ("Game", "Away", "Home", "Start (UTC)", "Reasons"),
        [
            (
                row["game_pk"],
                row["away_team"],
                row["home_team"],
                row["scheduled_start_utc"],
                _reason_text(row),
            )
            for row in rows
        ],
    )


def render_live_top3_markdown(payload: Mapping[str, object]) -> bytes:
    """Render only a validated payload, without inventing score fields for failures."""
    safe = validate_run_payload(payload)
    warnings = safe["warnings"]
    warning_lines = "\n".join(f"- {_escape_table(value)}" for value in warnings)
    if not warning_lines:
        warning_lines = "- none"
    sections = [
        "# Experimental MLB Daily Top 3",
        "",
        "> **RESEARCH ONLY** · **NON-OFFICIAL SAMPLE** · **PROFITABILITY NOT VALIDATED**",
        "",
        f'- Target date (Eastern calendar): `{safe["target_date"]}`',
        f'- Executed at Eastern (ET): `{safe["executed_at_eastern"]}`',
        f'- Executed at UTC: `{safe["executed_at_utc"]}`',
        f'- D-1 history cutoff: `{safe["history_cutoff_date"]}`',
        "",
        "## Verified Top 3",
        "",
        _ranked_table(safe["verified_top3"]),
        "",
        "## Preliminary Top 3",
        "",
        _ranked_table(safe["preliminary_top3"]),
        "",
        "## Unscored games",
        "",
        _unranked_table(safe["unscored_games"]),
        "",
        "## Excluded games",
        "",
        _unranked_table(safe["excluded_games"]),
        "",
        "## Warnings",
        "",
        warning_lines,
        "",
    ]
    return "\n".join(sections).encode("utf-8")


def _report_relative_path(
    payload: Mapping[str, object], extension: str
) -> Path:
    stamp = str(payload["executed_at_utc"])
    safe_stamp = stamp.replace("-", "").replace(":", "").replace(".", "")
    filename = f"{safe_stamp}-{str(payload['content_sha256'])[:16]}.{extension}"
    return _REPORT_ROOT / str(payload["target_date"]) / filename


def _readiness_relative_path(payload: Mapping[str, object]) -> Path:
    return _report_relative_path(payload, "md").with_suffix(".readiness.md")


def _binding_for_game(
    bindings: Sequence[Mapping[str, object]], role: str, game_pk: int
) -> Mapping[str, object] | None:
    matches = [
        binding
        for binding in bindings
        if binding.get("binding_role") == role
        and game_pk in binding.get("game_pks", ())
    ]
    if len(matches) > 1:
        raise IntegrityError("duplicate readiness source binding")
    return matches[0] if matches else None


def _readiness_binding_lines(
    label: str, binding: Mapping[str, object] | None
) -> list[str]:
    if binding is None:
        return [f"- {label}: unavailable"]
    return [
        f"- {label} observation ID: `{_escape_table(binding['observation_id'])}`",
        f"- {label} recorded observation time (UTC): `{_escape_table(binding['durable_at_utc'])}`",
        f"- {label} binding SHA-256: `{_escape_table(binding['binding_sha256'])}`",
        f"- {label} raw payload SHA-256: `{_escape_table(binding['payload_sha256'])}`",
        f"- {label} receipt SHA-256: `{_escape_table(binding['receipt_sha256'])}`",
    ]


def _readiness_expected(game: Mapping[str, object], side: str) -> str:
    pitcher_id = game.get(f"{side}_probable_pitcher_id")
    pitcher_name = game.get(f"{side}_probable_pitcher_name")
    id_text = str(pitcher_id) if type(pitcher_id) is int and pitcher_id > 0 else "unknown"
    name_text = str(pitcher_name) if isinstance(pitcher_name, str) and pitcher_name else "unknown"
    return f"{id_text} / {name_text}"


def _readiness_observed(participant: object, side: str) -> tuple[str, str]:
    if participant is None:
        return "unknown", "unknown"
    pitcher_id = getattr(participant, f"{side}_pitcher_list_id", None)
    lineup = getattr(participant, f"{side}_lineup", None)
    id_text = str(pitcher_id) if type(pitcher_id) is int and pitcher_id > 0 else "unknown"
    count_text = str(len(lineup)) if isinstance(lineup, Sequence) and not isinstance(lineup, (str, bytes)) else "unknown"
    return id_text, count_text


def _readiness_classification(row: Mapping[str, object] | None) -> str:
    if row is None:
        return "not listed in ranked output"
    if "rank" in row:
        return f'{row["state"]} (rank {row["rank"]})'
    return f'{row["state"]} (not ranked)'


def render_live_pregame_readiness_markdown(
    slate: object, validated_payload: Mapping[str, object]
) -> str:
    """Render a provenance-bound, non-scoring view of the captured pregame inputs."""
    safe = validate_run_payload(validated_payload)
    games = getattr(slate, "games", None)
    participants = getattr(slate, "participants", None)
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        raise IntegrityError("readiness slate games differ")
    if not isinstance(participants, Mapping):
        raise IntegrityError("readiness slate participants differ")
    bindings = safe["source_bindings"]
    if not isinstance(bindings, list) or any(not isinstance(item, Mapping) for item in bindings):
        raise IntegrityError("readiness source bindings differ")

    rows_by_game: dict[int, Mapping[str, object]] = {}
    for section in ("verified_top3", "preliminary_top3", "unscored_games", "excluded_games"):
        for row in safe[section]:
            rows_by_game[int(row["game_pk"])] = row
    schedule_binding = next(
        (binding for binding in bindings if binding.get("binding_role") == "LIVE_SCHEDULE"),
        None,
    )
    primary_relative = _report_relative_path(safe, "json")
    sections = [
        "# Experimental MLB Pregame Readiness",
        "",
        "> **RESEARCH ONLY** · **NOT A BETTING RECOMMENDATION** · **PARTICIPANT COMPLETENESS IS NOT SCORE READINESS**",
        "",
        "Participant completeness is not score-readiness and does not prove that all scoring gates pass. This companion documents observed inputs only; it is not a pick.",
        "",
        f'- Target date (Eastern calendar): `{_escape_table(safe["target_date"])}`',
        f'- Primary report filename: `{primary_relative.name}`',
        f'- Primary report: `{primary_relative.as_posix()}`',
        f'- Primary report content_sha256: `{safe["content_sha256"]}`',
        f'- Model card SHA-256: `{safe["model_card_sha256"]}`',
        "",
        "## Schedule observation",
        "",
    ]
    sections.extend(_readiness_binding_lines("Schedule", schedule_binding))
    sections.extend(("", "## Per-game readiness", ""))
    sections.append(
        _markdown_table(
            (
                "Game",
                "Away",
                "Home",
                "Scheduled start (UTC)",
                "Expected starters (ID / name)",
                "Observed pitcher-list IDs",
                "Validated lineup counts",
                "Classification",
            ),
            [
                (
                    game["game_pk"],
                    game["away_team"],
                    game["home_team"],
                    game["scheduled_start_utc"],
                    f'Away: {_readiness_expected(game, "away")}; Home: {_readiness_expected(game, "home")}',
                    f'Away: {_readiness_observed(participants.get(int(game["game_pk"])), "away")[0]}; Home: {_readiness_observed(participants.get(int(game["game_pk"])), "home")[0]}',
                    f'Away: {_readiness_observed(participants.get(int(game["game_pk"])), "away")[1]}; Home: {_readiness_observed(participants.get(int(game["game_pk"])), "home")[1]}',
                    _readiness_classification(rows_by_game.get(int(game["game_pk"]))),
                )
                for game in games
            ],
        )
    )
    for game in games:
        game_pk = int(game["game_pk"])
        participant_binding = _binding_for_game(bindings, "LIVE_PARTICIPANTS", game_pk)
        final_binding = _binding_for_game(bindings, "FINAL_SCHEDULE", game_pk)
        participant = participants.get(game_pk)
        observed_away, count_away = _readiness_observed(participant, "away") if participant_binding else ("unknown", "unknown")
        observed_home, count_home = _readiness_observed(participant, "home") if participant_binding else ("unknown", "unknown")
        row = rows_by_game.get(game_pk)
        classification = _readiness_classification(row)
        reasons = "none" if row is None else _reason_text(row)
        sections.extend(
            (
                "",
                f'### {_escape_table(game["game_pk"])} · {_escape_table(game["away_team"])} at {_escape_table(game["home_team"])}',
                "",
                f'- Expected away starter ID / name: `{_escape_table(_readiness_expected(game, "away"))}`',
                f'- Expected home starter ID / name: `{_escape_table(_readiness_expected(game, "home"))}`',
                f'- Scheduled start (UTC): `{_escape_table(game["scheduled_start_utc"])}`',
                f'- Expected away starter ID: `{_escape_table(game.get("away_probable_pitcher_id") if type(game.get("away_probable_pitcher_id")) is int and game.get("away_probable_pitcher_id") > 0 else "unknown")}`',
                f'- Expected home starter ID: `{_escape_table(game.get("home_probable_pitcher_id") if type(game.get("home_probable_pitcher_id")) is int and game.get("home_probable_pitcher_id") > 0 else "unknown")}`',
                f'- Expected away starter name: `{_escape_table(game.get("away_probable_pitcher_name") if isinstance(game.get("away_probable_pitcher_name"), str) and game.get("away_probable_pitcher_name") else "unknown")}`',
                f'- Expected home starter name: `{_escape_table(game.get("home_probable_pitcher_name") if isinstance(game.get("home_probable_pitcher_name"), str) and game.get("home_probable_pitcher_name") else "unknown")}`',
                f'- Observed away pitcher-list ID: `{_escape_table(observed_away)}`',
                f'- Observed home pitcher-list ID: `{_escape_table(observed_home)}`',
                f'- Validated away lineup count: `{_escape_table(count_away)}`',
                f'- Validated home lineup count: `{_escape_table(count_home)}`',
                f'- Primary report classification: `{_escape_table(classification)}`',
                f'- Primary report reasons: `{_escape_table(reasons)}`',
            )
        )
        sections.extend(_readiness_binding_lines("Participant", participant_binding))
        sections.extend(_readiness_binding_lines("Later final-schedule recheck", final_binding))
    sections.append("")
    return "\n".join(sections)


def persist_live_top3_report(
    root: Path, payload: Mapping[str, object]
) -> tuple[Path, Path]:
    """Append one immutable JSON/Markdown report pair under the trusted run root."""
    root_path = normalize_live_root_path(Path(root))
    with open_trusted_root(root_path) as trusted:
        return _persist_bound_live_top3_report(root_path, trusted, payload)


def _persist_bound_live_top3_report(
    root_path: Path,
    trusted: TrustedRoot,
    payload: Mapping[str, object],
) -> tuple[Path, Path]:
    safe = validate_run_payload(payload)
    json_contents = canonical_json_bytes(safe)
    json_relative = _report_relative_path(safe, "json")
    markdown_relative = _report_relative_path(safe, "md")
    trusted.assert_bound()
    json_snapshot = read_regular_snapshot(
        trusted,
        json_relative,
        "experimental daily Top 3 JSON",
        missing_ok=True,
    )
    markdown_snapshot = read_regular_snapshot(
        trusted,
        markdown_relative,
        "experimental daily Top 3 Markdown",
        missing_ok=True,
    )
    if markdown_snapshot is not None:
        raise FileExistsError("experimental daily Top 3 report already exists")
    if json_snapshot is None:
        json_snapshot = write_exclusive(
            trusted, json_relative, json_contents, "experimental daily Top 3 JSON"
        )
    elif json_snapshot.contents != json_contents:
        raise FileExistsError("experimental daily Top 3 JSON identity conflicts")
    rebind_regular_snapshot(
        trusted, json_snapshot, "experimental daily Top 3 JSON"
    )
    # Render from the canonical bytes just bound on disk, never from a mutable caller object.
    persisted_payload = validate_run_payload(json.loads(json_snapshot.contents))
    markdown_contents = render_live_top3_markdown(persisted_payload)
    markdown_snapshot = write_exclusive(
        trusted,
        markdown_relative,
        markdown_contents,
        "experimental daily Top 3 Markdown",
    )
    rebind_regular_snapshot(
        trusted, markdown_snapshot, "experimental daily Top 3 Markdown"
    )
    rebind_regular_snapshot(
        trusted, json_snapshot, "experimental daily Top 3 JSON"
    )
    trusted.assert_bound()
    return root_path / json_relative, root_path / markdown_relative


def _persist_bound_live_pregame_readiness_report(
    root_path: Path,
    trusted: TrustedRoot,
    slate: object,
    payload: Mapping[str, object],
) -> Path:
    safe = validate_run_payload(payload)
    json_relative = _report_relative_path(safe, "json")
    readiness_relative = _readiness_relative_path(safe)
    trusted.assert_bound()
    json_snapshot = read_regular_snapshot(
        trusted,
        json_relative,
        "experimental daily Top 3 JSON",
    )
    if json_snapshot is None:
        raise IntegrityError("experimental daily Top 3 JSON is missing")
    rebind_regular_snapshot(
        trusted, json_snapshot, "experimental daily Top 3 JSON"
    )
    persisted_payload = validate_run_payload(json.loads(json_snapshot.contents))
    readiness_snapshot = read_regular_snapshot(
        trusted,
        readiness_relative,
        "experimental daily pregame readiness Markdown",
        missing_ok=True,
    )
    if readiness_snapshot is not None:
        raise FileExistsError(
            "experimental daily pregame readiness report already exists"
        )
    readiness_contents = render_live_pregame_readiness_markdown(
        slate, persisted_payload
    ).encode("utf-8")
    readiness_snapshot = write_exclusive(
        trusted,
        readiness_relative,
        readiness_contents,
        "experimental daily pregame readiness Markdown",
    )
    rebind_regular_snapshot(
        trusted,
        readiness_snapshot,
        "experimental daily pregame readiness Markdown",
    )
    rebind_regular_snapshot(
        trusted, json_snapshot, "experimental daily Top 3 JSON"
    )
    trusted.assert_bound()
    return root_path / readiness_relative


def main(argv: Sequence[str] | None = None) -> int:
    """CLI exit policy: integrity 2, operations 1, interrupts 130, success 0."""
    try:
        args = parse_args(argv)
        payload, json_path, markdown_path = run_daily_top3(
            args.root, args.date, args.model_card
        )
        if args.json_only:
            sys.stdout.write(canonical_json_bytes(payload).decode("utf-8"))
        else:
            print(f"Verified Top 3: {len(payload['verified_top3'])}")
            print(f"Preliminary Top 3: {len(payload['preliminary_top3'])}")
            print(f"Unscored games: {len(payload['unscored_games'])}")
            print(f"Excluded games: {len(payload['excluded_games'])}")
            print(f"Warnings: {len(payload['warnings'])}")
            print(f"JSON report: {json_path.absolute()}")
            print(f"Markdown report: {markdown_path.absolute()}")
            print(
                "Readiness report: "
                f"{markdown_path.with_name(_readiness_relative_path(payload).name).absolute()}"
            )
        return 0
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    except KeyboardInterrupt:
        return 130
    except (IntegrityError, ValueError, json.JSONDecodeError) as exc:
        print(f"Daily Top 3 integrity failure: {exc}", file=sys.stderr)
        return 2
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
        print(f"Daily Top 3 operational failure: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Network clients may expose provider-specific exceptions.
        print(f"Daily Top 3 operational failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
