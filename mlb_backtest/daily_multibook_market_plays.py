"""Build and publish the provisional full-slate multi-book market report."""

from __future__ import annotations

import hashlib
import json
import os
import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .market_play_contracts import (
    POLICY,
    REASON_ORDER,
    canonical_json_bytes,
    canonical_utc,
    content_digest,
    sidecar_bytes,
    validate_relative_path,
    validate_policy,
)
from .live_top3_contracts import validate_target_date
from .multibook_market_board import load_market_board, load_schedule_authority
from .secure_fs import (
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


REPORT_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_PLAYS_V1"
TRACE_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_PLAYS_TRACE_V1"
STATUS_LABELS = ["RESEARCH ONLY", "PROVISIONAL", "NOT BET-VALIDATED"]
REPORT_ROOT = Path("reports/experimental_multibook_market_plays")


@dataclass(frozen=True, slots=True)
class BuiltMarketPlays:
    report: Mapping[str, object]
    report_bytes: bytes
    sidecar_bytes: bytes
    markdown_bytes: bytes
    trace: Mapping[str, object]
    trace_bytes: bytes
    authorities: tuple[object, ...]
    root: Path


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(root, relative: Path, label: str, max_bytes: int = 64 * 1024 * 1024):
    snapshot = read_regular_snapshot(root, relative, label, max_bytes=max_bytes)
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    try:
        value = json.loads(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return snapshot, value


def _relative(root: Path, path: Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = Path(os.path.abspath(candidate)).relative_to(Path(os.path.abspath(root)))
        except ValueError as exc:
            raise ValueError(f"{label} escapes root") from exc
    return validate_relative_path(candidate)


def _canonical_digest(value: Mapping[str, object]) -> str:
    return content_digest({key: item for key, item in value.items() if key != "content_sha256"})


def _section_row(row: Mapping[str, object]) -> dict[str, object]:
    copied = dict(row)
    reasons = copied.get("reason_codes")
    if not isinstance(reasons, list) or any(reason not in REASON_ORDER for reason in reasons):
        raise ValueError("market-play row reason codes differ")
    if "rank" in copied:
        raise ValueError("market-play rows must not contain rank")
    return copied


def validate_market_plays_report(value: object) -> dict[str, object]:
    required = {
        "schema", "content_sha256", "status_labels", "target_date", "evaluated_at_utc",
        "policy", "policy_sha256", "ml_trace_relative", "ml_trace_sha256",
        "totals_trace_relative", "totals_trace_sha256", "board_relative", "board_sha256",
        "moneylines", "totals", "warnings",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("market-play report fields differ")
    safe = json.loads(canonical_json_bytes(dict(value)))
    if safe["schema"] != REPORT_SCHEMA or safe["status_labels"] != STATUS_LABELS:
        raise ValueError("market-play report schema/status differs")
    canonical_utc(safe["evaluated_at_utc"])
    validate_policy(safe["policy"])
    if safe["policy_sha256"] != _sha(canonical_json_bytes(POLICY)):
        raise ValueError("market-play policy digest differs")
    for field in ("ml_trace_sha256", "totals_trace_sha256", "board_sha256"):
        if not isinstance(safe[field], str) or len(safe[field]) != 64:
            raise ValueError(f"market-play {field} differs")
    validate_target_date(safe["target_date"])
    for field in ("ml_trace_relative", "totals_trace_relative", "board_relative"):
        if not isinstance(safe[field], str):
            raise ValueError(f"market-play {field} differs")
        validate_relative_path(Path(safe[field]))
    if not isinstance(safe["moneylines"], list) or not isinstance(safe["totals"], list):
        raise ValueError("market-play sections differ")
    for section in (safe["moneylines"], safe["totals"]):
        for row in section:
            if not isinstance(row, Mapping) or "game_pk" not in row or "decision" not in row:
                raise ValueError("market-play row differs")
            _section_row(row)
    ml_pks = [row["game_pk"] for row in safe["moneylines"]]
    total_pks = [row["game_pk"] for row in safe["totals"]]
    if len(set(ml_pks)) != len(ml_pks) or ml_pks != total_pks:
        raise ValueError("market-play game populations differ")
    if not isinstance(safe["warnings"], list) or any(not isinstance(w, str) for w in safe["warnings"]):
        raise ValueError("market-play warnings differ")
    if safe["content_sha256"] != _canonical_digest(safe):
        raise ValueError("market-play report content digest differs")
    return safe


def validate_market_plays_trace(value: object) -> dict[str, object]:
    required = {"schema", "content_sha256", "report_relative", "report_sha256", "report_sidecar_sha256", "markdown_sha256", "evaluated_at_utc", "policy_sha256"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("market-play trace fields differ")
    safe = json.loads(canonical_json_bytes(dict(value)))
    if safe["schema"] != TRACE_SCHEMA:
        raise ValueError("market-play trace schema differs")
    if (
        not isinstance(safe["report_relative"], str)
        or not safe["report_relative"]
        or "\\" in safe["report_relative"]
    ):
        raise ValueError("market-play trace report path differs")
    validate_relative_path(Path(safe["report_relative"]))
    canonical_utc(safe["evaluated_at_utc"])
    for field in ("report_sha256", "report_sidecar_sha256", "markdown_sha256", "policy_sha256"):
        if not isinstance(safe[field], str) or len(safe[field]) != 64:
            raise ValueError(f"market-play trace {field} differs")
    if safe["content_sha256"] != _canonical_digest(safe):
        raise ValueError("market-play trace content digest differs")
    return safe


def render_market_plays_markdown(payload: object) -> bytes:
    report = validate_market_plays_report(payload)
    lines = [
        "# Provisional MLB Market Plays",
        "",
        "> **RESEARCH ONLY** · **PROVISIONAL** · **NOT BET-VALIDATED**",
        "",
        f"- Target date: `{report['target_date']}`",
        f"- Evaluated at (UTC): `{report['evaluated_at_utc']}`",
        "",
        "## ML PLAY / PASS",
        "",
        "| Game | Decision | Side | Probability | Best book | Price margin | Reasons |",
        "| --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for row in report["moneylines"]:
        lines.append(
            f"| {row['game_pk']} | {row['decision']} | {row.get('predicted_side') or '—'} | "
            f"{row.get('pick_probability') or '—'} | {row.get('selected_book') or '—'} | "
            f"{row.get('price_margin') or '—'} | {', '.join(row['reason_codes']) or '—'} |"
        )
    lines.extend(["", "## TOTAL PLAY / PASS", "", "| Game | Decision | Direction | Line | Book | Support margin | Reasons |", "| --- | --- | --- | ---: | --- | ---: | --- |"])
    for row in report["totals"]:
        lines.append(
            f"| {row['game_pk']} | {row['decision']} | {row.get('direction') or '—'} | "
            f"{row.get('line_half_runs') or '—'} | {row.get('selected_book') or '—'} | "
            f"{row.get('support_margin') or '—'} | {', '.join(row['reason_codes']) or '—'} |"
        )
    lines.extend(["", "This report contains provisional research designations and no wagering guarantee.", ""])
    return "\n".join(lines).encode("utf-8")


def _board_authority(root: Path, board_path: Path):
    with open_trusted_root(root) as trusted:
        _, board_payload = _read_json(trusted, board_path, "market board")
    schedule_relative = Path(str(board_payload["schedule_receipt_relative"]))
    schedule = load_schedule_authority(root, schedule_relative)
    return load_market_board(root, board_path, schedule)


def build_market_plays(
    root: Path,
    ml_trace: Path,
    totals_trace: Path,
    board: Path,
    *,
    evaluated_clock: Callable[[], str],
) -> BuiltMarketPlays:
    """Load sealed authorities once and build immutable report/trace bytes."""
    from .market_play_authorities import load_ml_authority, load_totals_authority
    from .multibook_ml_evaluator import evaluate_moneylines
    from .multibook_totals_evaluator import evaluate_totals

    root = Path(root)
    ml = load_ml_authority(root, ml_trace)
    totals = load_totals_authority(root, totals_trace)
    board_relative = _relative(root, Path(board), "market board")
    board_authority = _board_authority(root, board_relative)
    if ml.target_date != totals.target_date or ml.target_date != board_authority.target_date:
        raise ValueError("market-play authority target dates differ")
    evaluated_at = canonical_utc(evaluated_clock())
    participants = {game.game_pk: game.participant for game in ml.games}
    moneylines = evaluate_moneylines(ml, board_authority, evaluated_at=evaluated_at)
    total_rows = evaluate_totals(totals, participants, board_authority, evaluated_at=evaluated_at)
    ml_trace_path = _relative(root, Path(ml_trace), "ML trace")
    totals_trace_path = _relative(root, Path(totals_trace), "totals trace")
    board_path = board_relative
    with open_trusted_root(root) as trusted:
        def digest_path(path: Path, label: str) -> str:
            snapshot = read_regular_snapshot(trusted, path, label, max_bytes=64 * 1024 * 1024)
            if snapshot is None:
                raise ValueError(f"{label} is missing")
            return _sha(snapshot.contents)
        ml_trace_sha256 = digest_path(ml_trace_path, "ML trace")
        totals_trace_sha256 = digest_path(totals_trace_path, "totals trace")
        board_sha256 = digest_path(board_path, "market board")
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "content_sha256": "",
        "status_labels": STATUS_LABELS,
        "target_date": ml.target_date,
        "evaluated_at_utc": evaluated_at,
        "policy": POLICY,
        "policy_sha256": _sha(canonical_json_bytes(POLICY)),
        "ml_trace_relative": ml_trace_path.as_posix(),
        "ml_trace_sha256": ml_trace_sha256,
        "totals_trace_relative": totals_trace_path.as_posix(),
        "totals_trace_sha256": totals_trace_sha256,
        "board_relative": board_path.as_posix(),
        "board_sha256": board_sha256,
        "moneylines": [dict(row) for row in moneylines],
        "totals": [dict(row) for row in total_rows],
        "warnings": [],
    }
    report["content_sha256"] = _canonical_digest(report)
    report = validate_market_plays_report(report)
    report_bytes = canonical_json_bytes(report)
    markdown_bytes = render_market_plays_markdown(report)
    sidecar = sidecar_bytes(report_bytes)
    trace: dict[str, object] = {
        "schema": TRACE_SCHEMA,
        "content_sha256": "",
        "report_relative": f"{REPORT_ROOT.as_posix()}/{report['target_date']}/{report['target_date']}-{report['content_sha256']}.json",
        "report_sha256": _sha(report_bytes),
        "report_sidecar_sha256": _sha(sidecar),
        "markdown_sha256": _sha(markdown_bytes),
        "evaluated_at_utc": evaluated_at,
        "policy_sha256": report["policy_sha256"],
    }
    trace["content_sha256"] = _canonical_digest(trace)
    trace = validate_market_plays_trace(trace)
    trace_bytes = canonical_json_bytes(trace)
    return BuiltMarketPlays(
        MappingProxyType(report), report_bytes, sidecar, markdown_bytes,
        MappingProxyType(trace), trace_bytes, (ml, totals, board_authority), root,
    )


def _write_or_reuse(trusted, relative: Path, contents: bytes, label: str):
    existing = read_regular_snapshot(trusted, relative, label, max_bytes=max(64 * 1024 * 1024, len(contents)), missing_ok=True)
    if existing is not None:
        if existing.contents != contents:
            raise ValueError(f"conflicting append-only {label}")
        return existing
    return write_exclusive(trusted, relative, contents, label)


def persist_market_plays(root: Path, built: BuiltMarketPlays) -> tuple[Path, Path, Path, Path]:
    from .market_play_authorities import rebind_authorities

    if not isinstance(built, BuiltMarketPlays):
        raise TypeError("built market plays differs")
    report = validate_market_plays_report(built.report)
    if canonical_json_bytes(report) != built.report_bytes or sidecar_bytes(built.report_bytes) != built.sidecar_bytes:
        raise ValueError("built market-play report bytes differ")
    if canonical_json_bytes(dict(built.trace)) != built.trace_bytes:
        raise ValueError("built market-play trace bytes differ")
    if Path(os.path.abspath(built.root)) != Path(os.path.abspath(root)):
        raise ValueError("built market-play root differs")
    rebind_authorities(*built.authorities)
    # Treat every field supplied by the caller as an untrusted transport.  The
    # sealed timestamp and input references are the only values we reuse; all
    # decisions and derived bytes are rebuilt from the authorities before any
    # append-only output is touched.
    rebuilt = build_market_plays(
        Path(root),
        Path(str(report["ml_trace_relative"])),
        Path(str(report["totals_trace_relative"])),
        Path(str(report["board_relative"])),
        evaluated_clock=lambda: str(report["evaluated_at_utc"]),
    )
    if (
        rebuilt.report_bytes != built.report_bytes
        or rebuilt.sidecar_bytes != built.sidecar_bytes
        or rebuilt.markdown_bytes != built.markdown_bytes
        or rebuilt.trace_bytes != built.trace_bytes
    ):
        raise ValueError("rebuilt market-play bundle differs")
    rebind_authorities(*rebuilt.authorities)
    trace = dict(built.trace)
    report_digest = report["content_sha256"]
    stem = f"{report['target_date']}-{report_digest}"
    report_relative = REPORT_ROOT / report["target_date"] / f"{stem}.json"
    trace["report_relative"] = report_relative.as_posix()
    trace["content_sha256"] = _canonical_digest(trace)
    trace = validate_market_plays_trace(trace)
    trace_bytes = canonical_json_bytes(trace)
    if trace_bytes != rebuilt.trace_bytes:
        raise ValueError("rebuilt market-play trace bytes differ")
    markdown_bytes = render_market_plays_markdown(report)
    if markdown_bytes != rebuilt.markdown_bytes:
        raise ValueError("built market-play markdown bytes differ")
    with open_trusted_root(root) as trusted:
        input_snapshots = []
        for field, label in (
            ("ml_trace_relative", "ML trace"),
            ("totals_trace_relative", "totals trace"),
            ("board_relative", "market board"),
        ):
            snapshot = read_regular_snapshot(
                trusted, Path(str(report[field])), label, max_bytes=64 * 1024 * 1024
            )
            if snapshot is None or _sha(snapshot.contents) != report[field.replace("_relative", "_sha256")]:
                raise ValueError(f"{label} binding differs")
            input_snapshots.append((snapshot, label))
        output_snapshots = []
        output_snapshots.append((
            _write_or_reuse(trusted, report_relative.with_suffix(".sha256"), rebuilt.sidecar_bytes, "market-play report sidecar"),
            "market-play report sidecar",
        ))
        output_snapshots.append((
            _write_or_reuse(trusted, report_relative, rebuilt.report_bytes, "market-play report"),
            "market-play report",
        ))
        output_snapshots.append((
            _write_or_reuse(trusted, report_relative.with_suffix(".md"), rebuilt.markdown_bytes, "market-play markdown"),
            "market-play markdown",
        ))
        trace_relative = report_relative.with_suffix(".trace.json")
        output_snapshots.append((
            _write_or_reuse(trusted, trace_relative, trace_bytes, "market-play trace"),
            "market-play trace",
        ))
        # The trace is the sole success artifact.  Rebind every authority and
        # then reread all four outputs and their exact sidecar/digest links.
        rebind_authorities(*rebuilt.authorities)
        for snapshot, label in (*input_snapshots, *output_snapshots):
            rebind_regular_snapshot(trusted, snapshot, label)
        expected_outputs = (
            (report_relative.with_suffix(".sha256"), rebuilt.sidecar_bytes, "market-play report sidecar"),
            (report_relative, rebuilt.report_bytes, "market-play report"),
            (report_relative.with_suffix(".md"), rebuilt.markdown_bytes, "market-play markdown"),
            (trace_relative, trace_bytes, "market-play trace"),
        )
        for relative, expected, label in expected_outputs:
            snapshot = read_regular_snapshot(trusted, relative, label, max_bytes=max(64 * 1024 * 1024, len(expected)))
            if snapshot is None or snapshot.contents != expected:
                raise ValueError(f"{label} final binding differs")
        for field, label in (
            ("ml_trace_relative", "ML trace"),
            ("totals_trace_relative", "totals trace"),
            ("board_relative", "market board"),
        ):
            snapshot = read_regular_snapshot(
                trusted, Path(str(report[field])), label, max_bytes=64 * 1024 * 1024
            )
            if snapshot is None or _sha(snapshot.contents) != report[field.replace("_relative", "_sha256")]:
                raise ValueError(f"{label} final binding differs")
        trusted.assert_bound()
    return report_relative.with_suffix(".sha256"), report_relative, report_relative.with_suffix(".md"), trace_relative


__all__ = [
    "BuiltMarketPlays",
    "build_market_plays",
    "persist_market_plays",
    "render_market_plays_markdown",
    "validate_market_plays_report",
    "validate_market_plays_trace",
]


def main(argv: list[str] | None = None) -> int:
    """Evaluate sealed local evidence; never collect prices or run a model."""
    parser = argparse.ArgumentParser(description="evaluate offline multibook market plays")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--root", type=Path, required=True)
    evaluate.add_argument("--ml-trace", type=Path, required=True)
    evaluate.add_argument("--totals-trace", type=Path, required=True)
    evaluate.add_argument("--market-board", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        ml_trace = validate_relative_path(args.ml_trace)
        totals_trace = validate_relative_path(args.totals_trace)
        board = validate_relative_path(args.market_board)
        built = build_market_plays(
            args.root,
            ml_trace,
            totals_trace,
            board,
            evaluated_clock=lambda: _now_utc(),
        )
        _, _, _, trace_relative = persist_market_plays(args.root, built)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 2
    except KeyboardInterrupt:
        return 1
    except OSError:
        return 1
    print(trace_relative.as_posix())
    return 0


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
