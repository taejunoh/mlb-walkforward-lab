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


def _ranked_table(rows: Sequence[Mapping[str, object]]) -> str:
    return _markdown_table(
        ("Rank", "Game", "Pick", "Probability", "Completeness", "Start (UTC)", "Reasons"),
        [
            (
                row["rank"],
                f'{row["away_team"]} at {row["home_team"]}',
                row["predicted_winner"],
                row["pick_probability_display"],
                f'{float(row["data_completeness"]):.1%}',
                row["scheduled_start_utc"],
                _reason_text(row),
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
