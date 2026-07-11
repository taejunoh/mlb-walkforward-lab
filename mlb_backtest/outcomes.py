"""Post-lock result joins with no feature, scoring, or ranking dependencies."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from .locking import _canonical_jsonl, _parse_manifest


_REQUIRED = frozenset(("game_pk", "actual_winner", "status"))
_OPTIONAL = frozenset(
    (
        "market_snapshot_at",
        "market_source",
        "market_favorite",
        "away_no_vig_probability",
        "home_no_vig_probability",
    )
)
_ALLOWED = _REQUIRED | _OPTIONAL


def _read_regular(path: Path, label: str) -> bytes:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing {label}") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} integrity failure")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{label} integrity failure") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"{label} integrity failure")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_lock(manifest_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        manifest_bytes = _read_regular(manifest_path, "lock manifest")
        manifest = _parse_manifest(manifest_bytes)
        predictions_path = manifest_path.parent / "predictions.jsonl"
        predictions_bytes = _read_regular(predictions_path, "lock predictions")
        if hashlib.sha256(predictions_bytes).hexdigest() != manifest["sha256"]:
            raise RuntimeError("lock manifest predictions integrity failure")
        lines = predictions_bytes.splitlines()
        if len(lines) != manifest["row_count"]:
            raise RuntimeError("lock manifest row_count integrity failure")
        predictions = [json.loads(line.decode("utf-8")) for line in lines]
        if _canonical_jsonl(predictions) != predictions_bytes:
            raise RuntimeError("lock manifest canonical predictions integrity failure")
        if any(
            row.get("model_version") != manifest["model_version"]
            or row.get("protocol_phase") != manifest["protocol_phase"]
            for row in predictions
        ):
            raise RuntimeError("lock manifest model identity integrity failure")
        game_pks = [row.get("game_pk") for row in predictions]
        if len(game_pks) != len(set(game_pks)):
            raise RuntimeError("lock manifest duplicate prediction integrity failure")
        return manifest, predictions
    except RuntimeError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("lock manifest integrity failure") from exc


def _validate_outcome(source: Mapping[str, object], prediction: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(source, Mapping):
        raise ValueError("outcome rows must be mappings")
    keys = set(source)
    unknown = keys - _ALLOWED
    missing = _REQUIRED - keys
    if unknown:
        raise ValueError(f"unknown outcome fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing outcome fields: {sorted(missing)}")
    row = dict(source)
    game_pk = row["game_pk"]
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise ValueError("outcome game_pk must be a positive integer")
    if row["status"] not in ("Final", "VOID"):
        raise ValueError("outcome status must be Final or VOID")
    winner = row["actual_winner"]
    if not isinstance(winner, str):
        raise ValueError("actual_winner must be a string")
    if row["status"] == "Final" and winner not in (
        prediction["away_team"], prediction["home_team"]
    ):
        raise ValueError("Final actual_winner must equal a locked team name")
    if row["status"] == "VOID" and winner != "":
        raise ValueError("VOID actual_winner must be blank")
    for key in ("market_snapshot_at", "market_source", "market_favorite"):
        if key in row and not isinstance(row[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("away_no_vig_probability", "home_no_vig_probability"):
        if key in row:
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a finite probability")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{key} must be between 0 and 1")
    _canonical_jsonl([row])
    return row


def _atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("evaluated output must be a regular non-symlink file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def join_outcomes(
    lock_manifest_path: Path,
    outcome_rows: Iterable[Mapping[str, object]],
    out_dir: Path | None = None,
) -> Path:
    """Verify a lock, validate supplied outcomes, and write nested evaluated rows."""
    manifest_path = Path(lock_manifest_path)
    _, predictions = _load_lock(manifest_path)
    by_game = {row["game_pk"]: row for row in predictions}
    try:
        outcomes = list(outcome_rows)
    except TypeError as exc:
        raise ValueError("outcome_rows must be iterable mappings") from exc
    seen: set[int] = set()
    evaluated: list[dict[str, object]] = []
    for source in outcomes:
        if not isinstance(source, Mapping):
            raise ValueError("outcome rows must be mappings")
        game_pk = source.get("game_pk")
        if game_pk in seen:
            raise ValueError("duplicate outcome game_pk")
        if game_pk not in by_game:
            raise ValueError("outcome game_pk is absent from locked predictions")
        seen.add(game_pk)
        outcome = _validate_outcome(source, by_game[game_pk])
        evaluated.append(dict(by_game[game_pk], outcome=outcome))

    evaluated.sort(
        key=lambda row: (row["official_date"], row["daily_rank"], row["game_pk"])
    )
    output_root = manifest_path.parent if out_dir is None else Path(out_dir)
    if output_root.is_symlink():
        raise RuntimeError("evaluated output directory cannot be a symlink")
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "evaluated_predictions.jsonl"
    _atomic_write(output_path, _canonical_jsonl(evaluated))
    return output_path


__all__ = ["join_outcomes"]
