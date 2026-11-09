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
from .live_top3_trace import build_trace_payload, validate_trace_payload
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
    parser.add_argument("--competition-scope", choices=("regular-season", "postseason-research-v1"), default="regular-season")
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
    competition_scope: str = "regular-season",
) -> tuple[dict[str, object], Path, Path]:
    """Capture, score, and validate one fresh research-only live slate."""
    if competition_scope == "postseason-research-v1":
        return _run_postseason_research(root, target_date, model_card_path, fetcher=fetcher, utc_clock=utc_clock)
    if competition_scope != "regular-season":
        raise IntegrityError("competition scope differs")
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

        trace_sink: list[dict[str, object]] = []
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
            trace_sink=trace_sink,
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
        # Test seams may replace the service/validator with a deliberately
        # skeletal payload; a real validated run always has these fields.
        if isinstance(validated, Mapping) and {
            "content_sha256", "executed_at_utc", "target_date"
        } <= set(validated):
            _bound_call(
                trusted,
                _persist_bound_live_score_trace,
                root_path,
                trusted,
                validated,
                trace_sink,
            )
        trusted.assert_bound()
        return validated, json_path, markdown_path


def _run_postseason_research(root, target_date, model_card_path, *, fetcher, utc_clock):
    from .competition_policy import POSTSEASON_ML_RESEARCH_V1
    from .postseason_research_service import build_postseason_research_run
    from .postseason_research_contracts import build_postseason_trace, load_postseason_research
    target_date = validate_target_date(target_date)
    root_path, _, card_relative = _root_and_card_path(root, model_card_path)
    times = ExecutionTimes.capture(utc_clock)
    policy = POSTSEASON_ML_RESEARCH_V1
    with open_trusted_root(root_path) as trusted:
        card = _bound_call(trusted, _load_bound_model_card, trusted, card_relative)
        history = _bound_call(trusted, build_live_history, root_path, target_date, card,
                              fetcher=fetcher, expected_root_binding=trusted.binding, competition_policy=policy)
        slate = _bound_call(trusted, observe_live_slate, root_path, target_date,
                            fetcher=fetcher, expected_root_binding=trusted.binding, competition_policy=policy)
        saved = MappingProxyType({int(game["game_pk"]): game for game in slate.games})
        if len(saved) != len(slate.games):
            raise IntegrityError("research slate identity differs")
        career, career_bindings = _bound_call(trusted, load_live_career_bf, root_path, target_date,
            _observed_pitcher_ids(slate), fetcher=fetcher, expected_root_binding=trusted.binding, competition_policy=policy)
        def observe_final(pk, requested_date):
            if requested_date != target_date or pk not in saved:
                raise IntegrityError("research final request differs")
            return _bound_call(trusted, observe_final_game_schedule, root_path, requested_date, saved[pk],
                fetcher=fetcher, expected_root_binding=trusted.binding, competition_policy=policy)
        trace_rows = []
        payload = _bound_call(trusted, build_postseason_research_run, slate=slate, history=history, card=card,
            final_schedule_observer=observe_final, career_bf_by_pitcher=career, career_bf_bindings=career_bindings,
            command_inputs={"date": target_date, "model_card": card_relative, "competition_scope": "postseason-research-v1"},
            execution_times=times, trace_sink=trace_rows)
        stamp = payload["executed_at_utc"].replace("-", "").replace(":", "").replace(".", "")
        base = Path("reports/postseason_ml_research") / target_date / f"{stamp}-{payload['content_sha256'][:16]}.json"
        trace = build_postseason_trace(primary_report_relative=base.as_posix(), report=payload, games=trace_rows)
        artifacts = ((base, canonical_json_bytes(payload)),
                     (base.with_suffix(".md"), render_postseason_research_markdown(payload)),
                     (base.with_suffix(".trace.json"), canonical_json_bytes(trace)))
        import hashlib
        snapshots = []
        for relative, contents in artifacts:
            for output, raw in ((relative, contents), (relative.with_suffix(relative.suffix + ".sha256"), f"{hashlib.sha256(contents).hexdigest()}\n".encode())):
                current = read_regular_snapshot(trusted, output, "postseason research artifact", missing_ok=True)
                if current is None:
                    current = write_exclusive(trusted, output, raw, "postseason research artifact")
                elif current.contents != raw:
                    raise FileExistsError("postseason research artifact identity conflicts")
                snapshots.append(current)
        for snapshot in snapshots:
            rebind_regular_snapshot(trusted, snapshot, "postseason research artifact")
        _bound_call(trusted, load_postseason_research, root_path, base.with_suffix(".trace.json"))
        return payload, root_path / base, root_path / base.with_suffix(".md")


def render_postseason_research_markdown(payload):
    from .postseason_research_contracts import validate_postseason_report
    safe = validate_postseason_report(payload)
    rows = []
    for row in safe["games"]:
        rows.append((row["game_pk"], row["game_type"], f'{row["away_team"]} at {row["home_team"]}', row["state"],
                     "unavailable" if row["predicted_winner"] is None else row["predicted_winner"],
                     "unavailable" if row["p_home"] is None else f'{row["p_home"]:.6f}',
                     "unavailable" if row["pick_probability"] is None else f'{row["pick_probability"]:.1%}',
                     row["scheduled_start_utc"], ", ".join(row["reason_codes"])))
    sections = ["# MLB Postseason ML Research", "", "RESEARCH_ONLY · POSTSEASON_OUT_OF_DOMAIN · NOT_POSTSEASON_VALIDATED", "",
                "PASS — POSTSEASON_NOT_VALIDATED", "", "Market: NOT_EVALUATED", "Totals: NOT_EVALUATED — POSTSEASON_TOTALS_NOT_SUPPORTED", "",
                f'Target date: {safe["target_date"]}; D−1 cutoff: {safe["history_cutoff_date"]}',
                "VERIFIED means input readiness only, not postseason validity.",
                "Career BF and league ERA retain REGULAR_SEASON populations. Recent history uses same-season R/F/D/L/W through D−1; frozen windows are unchanged.", "",
                _markdown_table(("Game", "Type", "Matchup", "State", "Model winner", "P(home)", "Pick probability", "Start UTC", "Reasons"), rows), "", "## Warnings", ""]
    sections.extend(f"- {_escape_table(warning)}" for warning in safe["warnings"])
    return ("\n".join(sections) + "\n").encode()


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


_REASON_EXPLANATIONS = {
    "PROBABLE_CONSISTENT": "예상 선발과 관측 선발이 일치",
    "MISSING_AWAY_LINEUP": "원정 라인업 누락",
    "MISSING_HOME_LINEUP": "홈 라인업 누락",
    "MISSING_AWAY_PITCHER_LIST": "원정 선발 관측 누락",
    "MISSING_HOME_PITCHER_LIST": "홈 선발 관측 누락",
    "MISSING_SCHEDULE_PROBABLE": "공식 예상 선발 누락",
}
_FLAG_EXPLANATIONS = {
    "doubleheader_unsupported": "더블헤더 특성 미지원",
}
_MODEL_DISCLAIMER = (
    "내부 모델은 배당·O/U 입력을 지원하지 않으며, 이 판정은 연구용 "
    "사전 상태이지 베팅 추천이나 수익 보장이 아닙니다."
)


def _decision_explanation_lines(
    game: Mapping[str, object],
    row: Mapping[str, object] | None,
    participant: object,
) -> tuple[str, ...]:
    """Render deterministic, evidence-only Korean decision explanations."""
    classification = _readiness_classification(row)
    reasons = tuple(str(code) for code in (row or {}).get("reason_codes", ()))
    reason_text = tuple(
        _REASON_EXPLANATIONS.get(code, f"원본 코드: {code}")
        for code in reasons
    )
    flag_text: list[str] = []
    if row is not None:
        raw_flags = row.get("schedule_quality_flags") or {}
        if isinstance(raw_flags, Mapping):
            for side in ("away", "home"):
                for code in raw_flags.get(side, ()):
                    flag_text.append(
                        _FLAG_EXPLANATIONS.get(str(code), f"원본 코드: {code}")
                    )
    if row is None or row.get("state") in {"UNSCORED", "EXCLUDED"}:
        evidence = "확률을 산출하지 않음"
        risks = (*reason_text, *flag_text) or ("필수 입력 부족",)
    else:
        probability = float(row["pick_probability"])
        away_count = _readiness_observed(participant, "away")[1]
        home_count = _readiness_observed(participant, "home")[1]
        evidence = (
            f'{row["predicted_winner"]} 승리 확률 {probability:.1%}; '
            f"검증된 라인업 {away_count}명/{home_count}명"
        )
        if "PROBABLE_CONSISTENT" in reasons:
            evidence += "; 선발 예상 일치"
        risks = (
            f'데이터 완전성 {float(row["data_completeness"]):.1%}',
            *reason_text,
            *flag_text,
        )
    return (
        f"- 판정: `{_escape_table(classification)}`",
        f"- 근거: {_escape_table(evidence)}",
        f"- 리스크/보류 이유: {_escape_table('; '.join(risks) or 'none')}",
        f"- {_MODEL_DISCLAIMER}",
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
        sections.extend(
            _decision_explanation_lines(
                game,
                row,
                participant if participant_binding else None,
            )
        )
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


def _trace_relative_path(payload: Mapping[str, object]) -> Path:
    return _report_relative_path(payload, "json").with_suffix(".trace.json")


def persist_live_top3_trace(
    root: Path,
    payload: Mapping[str, object],
    trace_sink: Sequence[Mapping[str, object]],
) -> Path:
    """Append one immutable score-trace sidecar under the trusted run root."""
    root_path = normalize_live_root_path(Path(root))
    with open_trusted_root(root_path) as trusted:
        return _persist_bound_live_score_trace(
            root_path, trusted, payload, trace_sink
        )


def _persist_bound_live_score_trace(
    root_path: Path,
    trusted: TrustedRoot,
    payload: Mapping[str, object],
    trace_sink: Sequence[Mapping[str, object]],
) -> Path:
    """Persist an immutable trace bound to the primary report on disk."""
    safe = validate_run_payload(payload)
    primary_relative = _report_relative_path(safe, "json")
    trace_relative = _trace_relative_path(safe)
    trusted.assert_bound()
    primary_snapshot = read_regular_snapshot(
        trusted, primary_relative, "experimental daily Top 3 JSON"
    )
    if primary_snapshot is None:
        raise IntegrityError("experimental daily Top 3 JSON is missing")
    rebind_regular_snapshot(
        trusted, primary_snapshot, "experimental daily Top 3 JSON"
    )
    persisted_primary = validate_run_payload(json.loads(primary_snapshot.contents))
    if persisted_primary["content_sha256"] != safe["content_sha256"]:
        raise IntegrityError("trace primary report binding differs")
    trace = build_trace_payload(
        primary_report_relative=primary_relative.as_posix(),
        primary_report_content_sha256=persisted_primary["content_sha256"],
        model_card_sha256=persisted_primary["model_card_sha256"],
        games=trace_sink,
    )
    trace_contents = canonical_json_bytes(trace)
    existing = read_regular_snapshot(
        trusted,
        trace_relative,
        "experimental daily live score trace",
        missing_ok=True,
    )
    if existing is not None:
        if existing.contents != trace_contents:
            raise FileExistsError("experimental daily live score trace identity conflicts")
        trace_snapshot = existing
    else:
        trace_snapshot = write_exclusive(
            trusted,
            trace_relative,
            trace_contents,
            "experimental daily live score trace",
        )
    rebind_regular_snapshot(
        trusted, trace_snapshot, "experimental daily live score trace"
    )
    reread = read_regular_snapshot(
        trusted, trace_relative, "experimental daily live score trace"
    )
    if reread is None:
        raise IntegrityError("experimental daily live score trace is missing")
    rebind_regular_snapshot(
        trusted, reread, "experimental daily live score trace"
    )
    reread_trace = validate_trace_payload(
        json.loads(reread.contents),
        expected_primary_report_content_sha256=persisted_primary["content_sha256"],
        expected_model_card_sha256=persisted_primary["model_card_sha256"],
    )
    if (
        reread_trace["primary_report_relative"] != primary_relative.as_posix()
        or reread_trace["primary_report_content_sha256"]
        != persisted_primary["content_sha256"]
        or reread_trace["model_card_sha256"] != persisted_primary["model_card_sha256"]
    ):
        raise IntegrityError("trace report binding differs")
    trusted.assert_bound()
    return root_path / trace_relative


def main(argv: Sequence[str] | None = None) -> int:
    """CLI exit policy: integrity 2, operations 1, interrupts 130, success 0."""
    try:
        args = parse_args(argv)
        kwargs = {} if args.competition_scope == "regular-season" else {"competition_scope": args.competition_scope}
        payload, json_path, markdown_path = run_daily_top3(args.root, args.date, args.model_card, **kwargs)
        if args.json_only:
            sys.stdout.write(canonical_json_bytes(payload).decode("utf-8"))
        elif args.competition_scope == "postseason-research-v1":
            print("RESEARCH_ONLY · POSTSEASON_OUT_OF_DOMAIN · NOT_POSTSEASON_VALIDATED")
            print("PASS — POSTSEASON_NOT_VALIDATED")
            print(f"Full slate: {len(payload['games'])}")
            print(f"JSON report: {json_path.absolute()}")
            print(f"Markdown report: {markdown_path.absolute()}")
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
