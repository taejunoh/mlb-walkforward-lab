"""Immutable, deterministic prediction locks for the v5.1 backtest."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


LOCK_REQUIRED_FIELDS = (
    "game_pk",
    "official_date",
    "scheduled_start_utc",
    "lock_at_utc",
    "away_team",
    "home_team",
    "predicted_side",
    "predicted_winner",
    "p_home",
    "pick_probability",
    "confidence",
    "uncertainty",
    "tier",
    "full_agreement",
    "model_version",
    "protocol_phase",
    "daily_rank",
    "top1",
    "top3",
    "starter_edge_abs",
    "overall_completeness",
)

_COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")
LOCK_OPTIONAL_FIELDS = tuple(
    field
    for component in _COMPONENTS
    for field in (
        f"away_{component}_score",
        f"home_{component}_score",
        f"{component}_score",
        f"{component}_edge",
        f"{component}_quality",
    )
) + ("lineup_provenance", "data_status", "doubleheader_marker")
_REQUIRED = frozenset(LOCK_REQUIRED_FIELDS)
_OPTIONAL = frozenset(LOCK_OPTIONAL_FIELDS)
_ALLOWED = _REQUIRED | _OPTIONAL
_MANIFEST_FIELDS = frozenset(
    (
        "predictions_file",
        "sha256",
        "row_count",
        "model_version",
        "protocol_phase",
        "model_card_sha256",
        "schema_version",
        "created_at_utc",
    )
)
_HEX = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> str:
    """Serialize JSON with sorted keys and fixed-width, unquoted floats."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        raise ValueError("bool values are not permitted in canonical lock JSON")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite values are not permitted in canonical lock JSON")
        return format(value, ".10f")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_jsonl(rows: Iterable[Mapping[str, object]]) -> bytes:
    return "".join(f"{_canonical_json(row)}\n" for row in rows).encode("utf-8")


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _number(value: object, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{label} must be a finite number")
    if not low <= converted <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return converted


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _canonical_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("official_date must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("official_date must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("official_date must be a canonical ISO date")
    return value


def _canonical_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UTC datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a canonical UTC datetime")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise ValueError(f"{label} must be canonical UTC ending in Z")
    return parsed


def _validate_prediction(source: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(source, Mapping):
        raise ValueError("prediction rows must be mappings")
    keys = set(source)
    unknown = keys - _ALLOWED
    missing = _REQUIRED - keys
    if unknown:
        raise ValueError(f"unknown lock fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing lock fields: {sorted(missing)}")

    row = dict(source)
    _integer(row["game_pk"], "game_pk", positive=True)
    _canonical_date(row["official_date"])
    start = _canonical_utc(row["scheduled_start_utc"], "scheduled_start_utc")
    lock = _canonical_utc(row["lock_at_utc"], "lock_at_utc")
    if start - lock != timedelta(minutes=45):
        raise ValueError("lock_at_utc must be exactly 45 minutes before scheduled start")
    away = _nonempty_string(row["away_team"], "away_team")
    home = _nonempty_string(row["home_team"], "home_team")
    if away == home:
        raise ValueError("away_team and home_team must differ")

    side = row["predicted_side"]
    if side not in ("away", "home"):
        raise ValueError("predicted_side must be away or home")
    expected_winner = home if side == "home" else away
    if row["predicted_winner"] != expected_winner:
        raise ValueError("predicted_winner is inconsistent with predicted_side")
    p_home = _number(row["p_home"], "p_home", 0.1, 0.9)
    pick_probability = _number(
        row["pick_probability"], "pick_probability", 0.5, 0.9
    )
    if pick_probability <= 0.5:
        raise ValueError("pick_probability must be greater than 0.5")
    expected_probability = p_home if side == "home" else 1.0 - p_home
    if not math.isclose(
        pick_probability,
        expected_probability,
        rel_tol=0.0,
        abs_tol=1e-10,
    ):
        raise ValueError("pick_probability is inconsistent with p_home and predicted_side")
    if side == "home" and p_home <= 0.5 or side == "away" and p_home >= 0.5:
        raise ValueError("predicted_side is inconsistent with p_home")
    locked_p_home = float(format(p_home, ".10f"))
    locked_pick_probability = float(format(pick_probability, ".10f"))
    locked_expected_probability = (
        locked_p_home if side == "home" else 1.0 - locked_p_home
    )
    if locked_pick_probability <= 0.5 or (
        side == "home" and locked_p_home <= 0.5
    ) or (side == "away" and locked_p_home >= 0.5) or abs(
        Decimal(format(locked_pick_probability, ".10f"))
        - Decimal(format(locked_expected_probability, ".10f"))
    ) > Decimal("0.0000000001"):
        raise ValueError("probability loses winner-side consistency at lock precision")

    confidence = _number(row["confidence"], "confidence", 0.0, 10.0)
    if row["uncertainty"] not in ("High", "Medium", "Low"):
        raise ValueError("uncertainty must be High, Medium, or Low")
    if row["tier"] not in ("S", "A", "B", "Lean", "Toss-up"):
        raise ValueError("tier is invalid")
    if row["uncertainty"] == "High" or pick_probability < 0.54:
        expected_tier = "Toss-up"
    elif pick_probability >= 0.68 and confidence >= 9.0:
        expected_tier = "S"
    elif pick_probability >= 0.63 and confidence >= 8.5:
        expected_tier = "A"
    elif pick_probability >= 0.58 and confidence >= 8.0:
        expected_tier = "B"
    else:
        expected_tier = "Lean"
    if row["tier"] != expected_tier:
        raise ValueError("tier is inconsistent with frozen probability rules")
    if row["full_agreement"] not in ("Yes", "No"):
        raise ValueError("full_agreement must be Yes or No")
    if row["model_version"] != "v5.1-BT":
        raise ValueError("model_version must be v5.1-BT")
    if row["protocol_phase"] != "Backtest":
        raise ValueError("protocol_phase must be Backtest")

    rank = _integer(row["daily_rank"], "daily_rank", positive=True)
    expected_top1 = "Yes" if rank == 1 else "No"
    expected_top3 = "Yes" if rank <= 3 else "No"
    if row["top1"] != expected_top1:
        raise ValueError("top1 is inconsistent with daily_rank")
    if row["top3"] != expected_top3:
        raise ValueError("top3 is inconsistent with daily_rank")
    _number(row["starter_edge_abs"], "starter_edge_abs", 0.0, math.inf)
    _number(row["overall_completeness"], "overall_completeness", 0.0, 1.0)

    for component in _COMPONENTS:
        for score_field in (
            f"away_{component}_score",
            f"home_{component}_score",
            f"{component}_score",
        ):
            if score_field in row:
                _number(row[score_field], score_field, 0.0, 100.0)
        if f"{component}_edge" in row:
            _number(row[f"{component}_edge"], f"{component}_edge", -100.0, 100.0)
        if f"{component}_quality" in row:
            _number(row[f"{component}_quality"], f"{component}_quality", 0.0, 1.0)
    if "lineup_provenance" in row and not isinstance(row["lineup_provenance"], (str, Mapping, list, tuple)):
        raise ValueError("lineup_provenance must be JSON text or structured data")
    if "data_status" in row and not isinstance(row["data_status"], (str, Mapping, list, tuple)):
        raise ValueError("data_status must be JSON text or structured data")
    if "doubleheader_marker" in row and row["doubleheader_marker"] not in (
        "GAME1",
        "GAME2",
    ):
        raise ValueError("doubleheader_marker must be GAME1 or GAME2")
    _canonical_json(row)
    return row


def validate_lock_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate lock schema/invariants and return canonical row order."""
    if rows is None:
        raise ValueError("rows must be iterable mappings")
    try:
        validated = [_validate_prediction(row) for row in rows]
    except TypeError as exc:
        raise ValueError("rows must be iterable mappings") from exc

    game_pks = [row["game_pk"] for row in validated]
    if len(game_pks) != len(set(game_pks)):
        raise ValueError("game_pk values must be unique")
    by_date: dict[str, list[dict[str, object]]] = {}
    for row in validated:
        by_date.setdefault(row["official_date"], []).append(row)
    for official_date, daily_rows in by_date.items():
        expected_order = sorted(
            daily_rows,
            key=lambda row: (
                -float(row["pick_probability"]),
                -float(row["confidence"]),
                -float(row["starter_edge_abs"]),
                _canonical_utc(
                    row["scheduled_start_utc"], "scheduled_start_utc"
                ),
                row["game_pk"],
            ),
        )
        for expected_rank, row in enumerate(expected_order, 1):
            if row["daily_rank"] != expected_rank:
                raise ValueError(
                    f"daily_rank does not match frozen rank order for {official_date}"
                )
    return sorted(
        validated,
        key=lambda row: (row["official_date"], row["daily_rank"], row["game_pk"]),
    )


def _prepare_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("lock output directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("lock output must be a real directory")
    return path.resolve(strict=True)


def _safe_read(path: Path, parent: Path, label: str, *, missing_ok: bool = False) -> bytes | None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeError(f"missing {label}")
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")
    if path.resolve(strict=True).parent != parent:
        raise ValueError(f"{label} must be directly under the lock directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"{label} changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_once(path: Path, content: bytes, parent: Path, label: str) -> None:
    existing = _safe_read(path, parent, label, missing_ok=True)
    if existing is not None:
        if existing != content:
            raise RuntimeError(f"immutable {label} differs")
        return
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = _safe_read(path, parent, label)
            if existing != content:
                raise RuntimeError(f"immutable {label} differs")
    finally:
        temporary.unlink(missing_ok=True)


def _open_lock(path: Path, parent: Path):
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError("lock file must be a regular non-symlink file")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValueError("lock file cannot be opened safely") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or path.resolve(strict=True).parent != parent:
        os.close(descriptor)
        raise ValueError("lock file is unsafe")
    return os.fdopen(descriptor, "r+")


def _parse_manifest(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("lock manifest integrity failure") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise RuntimeError("lock manifest fields are invalid")
    try:
        if value["predictions_file"] != "predictions.jsonl":
            raise ValueError
        _sha256(value["sha256"], "sha256")
        _sha256(value["model_card_sha256"], "model_card_sha256")
        if _integer(value["row_count"], "row_count") < 0:
            raise ValueError
        if _integer(value["schema_version"], "schema_version") != 1:
            raise ValueError
        if value["model_version"] != "v5.1-BT" or value["protocol_phase"] != "Backtest":
            raise ValueError
        timestamp = _canonical_utc(value["created_at_utc"], "created_at_utc")
        if timestamp.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("lock manifest integrity failure") from exc
    return value


def lock_predictions(
    rows: Iterable[Mapping[str, object]],
    out_dir: Path,
    model_card_sha256: str,
) -> dict[str, object]:
    """Validate and immutably publish canonical prediction rows and a manifest."""
    model_hash = _sha256(model_card_sha256, "model_card_sha256")
    ordered = validate_lock_rows(rows)
    payload = _canonical_jsonl(ordered)
    digest = hashlib.sha256(payload).hexdigest()
    root = Path(out_dir)
    resolved_root = _prepare_directory(root)
    predictions_path = root / "predictions.jsonl"
    manifest_path = root / "lock_manifest.json"
    lock_path = root / ".lock"

    with _open_lock(lock_path, resolved_root) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            existing_manifest_bytes = _safe_read(
                manifest_path, resolved_root, "lock manifest", missing_ok=True
            )
            if existing_manifest_bytes is not None:
                manifest = _parse_manifest(existing_manifest_bytes)
                existing_predictions = _safe_read(predictions_path, resolved_root, "predictions")
                if hashlib.sha256(existing_predictions).hexdigest() != manifest["sha256"]:
                    raise RuntimeError("lock manifest predictions integrity failure")
                expected = {
                    "sha256": digest,
                    "row_count": len(ordered),
                    "model_card_sha256": model_hash,
                }
                if any(manifest[key] != value for key, value in expected.items()) or existing_predictions != payload:
                    raise RuntimeError("existing immutable lock differs")
                return manifest

            existing_predictions = _safe_read(
                predictions_path, resolved_root, "predictions", missing_ok=True
            )
            if existing_predictions is not None and existing_predictions != payload:
                raise RuntimeError("existing immutable predictions differ")
            manifest = {
                "predictions_file": "predictions.jsonl",
                "sha256": digest,
                "row_count": len(ordered),
                "model_version": "v5.1-BT",
                "protocol_phase": "Backtest",
                "model_card_sha256": model_hash,
                "schema_version": 1,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            _write_once(predictions_path, payload, resolved_root, "predictions")
            _write_once(manifest_path, manifest_bytes, resolved_root, "lock manifest")
            return manifest
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "LOCK_OPTIONAL_FIELDS",
    "LOCK_REQUIRED_FIELDS",
    "lock_predictions",
    "validate_lock_rows",
]
