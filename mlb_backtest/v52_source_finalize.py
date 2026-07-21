"""Command line interface for staged v5.2 source finalization.

The CLI is intentionally a thin adapter around the public services in
``v52_source_orchestration``.  It does not perform network or source I/O.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .v52_source_orchestration import (
    AdvanceRequest,
    CommonArgs,
    FinalizationContext,
    FinalizationError,
    ReplayRequest,
    ReviewRequest,
    advance_sources_ready,
    build_context,
    publish_inventory,
    read_status,
    run_preflight,
    write_replay,
    write_review,
)


SCHEMA_VERSION = "v5.2-source-finalize-cli/v1"
DEFAULT_RUN_SPEC = Path("protocols/v5_2_qd/historical_walkforward_500.json")
DEFAULT_CONTRACT = Path("protocols/v5_2_qd/historical_source_acquisition.json")
DEFAULT_PROTECTED = Path("audit/v5_1_bt/protected_raw_inventory.json")
_COMMANDS = frozenset(("status", "preflight", "publish", "review", "replay", "advance"))


def canonical_json(value: object) -> str:
    """Render one stable JSON document suitable for machine consumers."""
    return json.dumps(
        _json_safe(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe(item) for item in value]
        return sorted(normalized, key=_canonical_sort_key)
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_sort_key(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class CliUsageError(Exception):
    """An argument error that should be rendered by :func:`main`."""


class FinalizationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = FinalizationArgumentParser(
        prog="python3 -m mlb_backtest.v52_source_finalize"
    )
    common = FinalizationArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path("."))
    common.add_argument("--run-spec", type=Path, default=DEFAULT_RUN_SPEC)
    common.add_argument("--acquisition-contract", type=Path, default=DEFAULT_CONTRACT)
    common.add_argument("--protected-inventory", type=Path, default=DEFAULT_PROTECTED)
    common.add_argument("--json", action="store_true")
    common.add_argument("--quiet", action="store_true")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", parents=[common])
    commands.add_parser("preflight", parents=[common])
    publish = commands.add_parser("publish", parents=[common])
    publish.add_argument("--lock-owner", required=True)
    review = commands.add_parser("review", parents=[common])
    review.add_argument("--reviewer-identity", required=True)
    review.add_argument("--reviewer-role", required=True)
    review.add_argument("--decision", choices=("APPROVED", "REJECTED"), required=True)
    review.add_argument("--query-ids-file", type=Path, required=True)
    review.add_argument("--write-receipt", type=Path, required=True)
    review.add_argument("--write-commit", type=Path, required=True)
    replay = commands.add_parser("replay", parents=[common])
    replay.add_argument("--write-receipt", type=Path, required=True)
    replay.add_argument("--write-commit", type=Path, required=True)
    advance = commands.add_parser("advance", parents=[common])
    advance.add_argument("--review-receipt", type=Path, action="append", required=True)
    advance.add_argument("--replay-receipt", type=Path, action="append", required=True)
    advance.add_argument("--checkpoint", type=Path, required=True)
    advance.add_argument("--inventory", type=Path, required=True)
    return parser


_SINGLE_VALUE_OPTIONS = frozenset(
    {
        "--root",
        "--run-spec",
        "--acquisition-contract",
        "--protected-inventory",
        "--lock-owner",
        "--reviewer-identity",
        "--reviewer-role",
        "--decision",
        "--query-ids-file",
        "--write-receipt",
        "--write-commit",
        "--checkpoint",
        "--inventory",
    }
)


def _reject_conflicting_options(argv: Sequence[str]) -> None:
    observed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        raw = argv[index]
        option, separator, attached = raw.partition("=")
        if option in _SINGLE_VALUE_OPTIONS:
            if separator:
                value = attached
                consumed = 1
            else:
                if index + 1 >= len(argv):
                    raise CliUsageError(f"{option} requires a value")
                value = argv[index + 1]
                consumed = 2
            if option in observed and observed[option] != value:
                raise CliUsageError(f"{option} has conflicting values")
            observed[option] = value
            index += consumed
        else:
            index += 1


def _common_args(namespace: argparse.Namespace) -> CommonArgs:
    return CommonArgs(
        root=namespace.root,
        run_spec=namespace.run_spec,
        acquisition_contract=namespace.acquisition_contract,
        protected_inventory=namespace.protected_inventory,
    )


def _validate_namespace(namespace: argparse.Namespace) -> None:
    """Validate normalized argument relationships before any service call."""
    if namespace.command != "advance":
        return
    review_receipts = tuple(namespace.review_receipt or ())
    replay_receipts = tuple(namespace.replay_receipt or ())
    if not review_receipts:
        raise CliUsageError("advance requires at least one --review-receipt value")
    if len(replay_receipts) != 2:
        raise CliUsageError("advance requires exactly two --replay-receipt values")


def _build_context(common: CommonArgs) -> FinalizationContext:
    try:
        return build_context(common)
    except (OSError, ValueError) as exc:
        raise FinalizationError(
            "AUTHORITY_MALFORMED",
            "finalization authority is unavailable or unsafe",
            (str(common.root),),
            3,
        ) from exc


def _dispatch(namespace: argparse.Namespace) -> tuple[object, str]:
    common = _common_args(namespace)
    context = _build_context(common)
    command = namespace.command
    if command == "status":
        report = read_status(context)
        return report, report.phase
    if command == "preflight":
        report = run_preflight(context)
        return report, report.phase
    if command == "publish":
        return publish_inventory(context, lock_owner=namespace.lock_owner), context.phase
    if command == "review":
        request = ReviewRequest(
            reviewer_identity=namespace.reviewer_identity,
            reviewer_role=namespace.reviewer_role,
            decision=namespace.decision,
            query_ids_file=namespace.query_ids_file,
            write_receipt=namespace.write_receipt,
            write_commit=namespace.write_commit,
        )
        return write_review(context, request), context.phase
    if command == "replay":
        request = ReplayRequest(
            write_receipt=namespace.write_receipt,
            write_commit=namespace.write_commit,
        )
        return write_replay(context, request), context.phase
    if command == "advance":
        review_receipts = tuple(namespace.review_receipt or ())
        replay_receipts = tuple(namespace.replay_receipt or ())
        request = AdvanceRequest(
            review_receipts=review_receipts,
            replay_receipts=(replay_receipts[0], replay_receipts[1]),
            checkpoint=namespace.checkpoint,
            inventory=namespace.inventory,
        )
        report = advance_sources_ready(context, request)
        return report, report.phase
    raise CliUsageError(f"unknown command: {command}")


def _command_from_argv(argv: Sequence[str]) -> str:
    for token in argv:
        if token in _COMMANDS:
            return token
    return "unknown"


def _emit_success(command: str, phase: str, result: object, *, machine: bool, quiet: bool) -> None:
    if machine:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "status": "OK",
            "phase": phase,
            "result": result,
        }
        print(canonical_json(payload))
    elif not quiet:
        print(
            f"schema_version={SCHEMA_VERSION} command={command} "
            f"status=OK phase={phase} result={canonical_json(result)}"
        )


def _emit_failure(
    command: str,
    error_code: str,
    message: str,
    paths: Sequence[object],
    exit_code: int,
    *,
    machine: bool,
    parser: argparse.ArgumentParser | None = None,
) -> int:
    if machine:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "status": "ERROR",
            "error_code": error_code,
            "message": message,
            "paths": paths,
        }
        print(canonical_json(payload))
    else:
        if parser is not None:
            print(parser.format_usage().strip(), file=sys.stderr)
        print(f"error: {message}", file=sys.stderr)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    machine = "--json" in raw
    command = _command_from_argv(raw)
    parser = _parser()
    try:
        _reject_conflicting_options(raw)
        namespace = parser.parse_args(raw)
        command = namespace.command
        machine = bool(namespace.json)
        _validate_namespace(namespace)
        result, phase = _dispatch(namespace)
    except CliUsageError as exc:
        return _emit_failure(
            command,
            "USAGE_ERROR",
            str(exc),
            (),
            2,
            machine=machine,
            parser=parser,
        )
    except FinalizationError as exc:
        return _emit_failure(
            command,
            exc.error_code,
            exc.message,
            exc.paths,
            exc.exit_code,
            machine=machine,
        )
    except KeyboardInterrupt:
        return 130
    _emit_success(command, phase, result, machine=machine, quiet=bool(namespace.quiet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
