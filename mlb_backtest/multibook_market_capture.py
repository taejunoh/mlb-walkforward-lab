"""Admission of externally collected five-book market evidence.

The collector is intentionally out of process.  This module only validates
and content-addresses files already present below a caller-supplied evidence
root.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from .market_play_contracts import (
    SUPPORTED_BOOK_IDS,
    canonical_json_bytes,
    canonical_utc,
    canonical_utc_ns,
    content_digest,
    sidecar_bytes,
    validate_american_price,
    validate_line_half_runs,
    validate_relative_path,
)
from .secure_fs import (
    RegularFileSnapshot,
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


RAW_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_RAW_CAPTURE_V1"
OUTCOME_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_CAPTURE_OUTCOME_V1"
UNAVAILABLE_SCHEMA = "PROVISIONAL_MULTIBOOK_MARKET_UNAVAILABLE_V1"
RAW_PREFIX = PurePosixPath("data/experimental_multibook_market/raw")
OUTCOME_PREFIX = PurePosixPath("audit/experimental_multibook_market/capture_outcomes")
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REASONS = frozenset(
    {
        "SOURCE_UNAVAILABLE",
        "LOGIN_REQUIRED",
        "MARKET_NOT_POSTED",
        "TRANSPORT_FAILURE",
        "COLLECTOR_REJECTED",
    }
)
SOURCE_HOSTS = {
    "FANDUEL": frozenset({"sportsbook.fanduel.com"}),
    "DRAFTKINGS": frozenset({"sportsbook.draftkings.com"}),
    "BETMGM": frozenset({"sports.betmgm.com"}),
    "CAESARS": frozenset({"www.caesars.com", "sportsbook.caesars.com"}),
    "FANATICS": frozenset({"sportsbook.fanatics.com"}),
}


@dataclass(frozen=True, slots=True)
class LoadedCaptureOutcome:
    relative: Path
    payload: dict[str, object]
    snapshot: RegularFileSnapshot
    sidecar_snapshot: RegularFileSnapshot
    raw_snapshot: RegularFileSnapshot | None = None
    raw_sidecar_snapshot: RegularFileSnapshot | None = None

    @property
    def status(self) -> Literal["AVAILABLE", "UNAVAILABLE"]:
        return self.payload["status"]  # type: ignore[return-value]


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise ValueError(
            f"{label} keys differ; missing={sorted(keys - actual)} "
            f"unknown={sorted(actual - keys)}"
        )
    return dict(value)


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _load_json(snapshot: RegularFileSnapshot, label: str) -> dict[str, object]:
    try:
        value = json.loads(snapshot.contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_source_url(source_book: str, source_url: object) -> str:
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise ValueError("source_url must be HTTPS")
    parsed = urlsplit(source_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("source_url must not contain credentials/query/fragment")
    if parsed.hostname not in SOURCE_HOSTS[source_book] or not parsed.path:
        raise ValueError("source_url host differs from fixed sportsbook authority")
    return source_url


def _validate_event(event: object, index: int, source_book: str) -> dict[str, object]:
    item = _exact(
        event,
        {
            "book_event_id",
            "away_team",
            "home_team",
            "scheduled_start_utc",
            "moneyline",
            "totals",
        },
        f"events[{index}]",
    )
    if not isinstance(item["book_event_id"], str) or not item["book_event_id"]:
        raise ValueError(f"events[{index}].book_event_id must be non-empty")
    for field in ("away_team", "home_team"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise ValueError(f"events[{index}].{field} must be non-empty")
    canonical_utc(item["scheduled_start_utc"])
    moneyline = _exact(
        item["moneyline"],
        {"away_american_odds", "home_american_odds"},
        f"events[{index}].moneyline",
    )
    validate_american_price(moneyline["away_american_odds"])
    validate_american_price(moneyline["home_american_odds"])
    totals = item["totals"]
    if not isinstance(totals, list):
        raise ValueError(f"events[{index}].totals must be a list")
    lines: set[int] = set()
    for line_index, total in enumerate(totals):
        total_item = _exact(
            total,
            {"line_half_runs", "over_american_odds", "under_american_odds"},
            f"events[{index}].totals[{line_index}]",
        )
        line = validate_line_half_runs(total_item["line_half_runs"])
        if line in lines:
            raise ValueError("duplicate total line within one event")
        lines.add(line)
        validate_american_price(total_item["over_american_odds"])
        validate_american_price(total_item["under_american_odds"])
    del source_book  # reserved for future per-book event contracts
    return item


def validate_raw_market_capture(value: object) -> dict[str, object]:
    item = _exact(
        value,
        {
            "schema",
            "content_sha256",
            "source_book",
            "captured_at_utc",
            "source_url",
            "transport_metadata",
            "events",
        },
        "raw_capture",
    )
    if item["schema"] != RAW_SCHEMA:
        raise ValueError("raw capture schema differs")
    digest = _sha(item["content_sha256"], "raw_capture.content_sha256")
    without = dict(item)
    without["content_sha256"] = ""
    if digest != content_digest(without):
        raise ValueError("raw capture content_sha256 differs")
    source_book = item["source_book"]
    if source_book not in SUPPORTED_BOOK_IDS:
        raise ValueError("raw capture source_book is unsupported")
    canonical_utc(item["captured_at_utc"])
    if canonical_utc_ns(item["captured_at_utc"]) > canonical_utc_ns(_now_utc()):
        raise ValueError("raw capture timestamp is in the future")
    _validate_source_url(source_book, item["source_url"])
    metadata = item["transport_metadata"]
    if not isinstance(metadata, Mapping) or not all(isinstance(k, str) for k in metadata):
        raise ValueError("transport_metadata must be a string-keyed object")
    events = item["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("raw capture events must be a non-empty list")
    event_ids: set[str] = set()
    for index, event in enumerate(events):
        checked = _validate_event(event, index, source_book)
        event_id = checked["book_event_id"]
        assert isinstance(event_id, str)
        if event_id in event_ids:
            raise ValueError("duplicate book event ID")
        event_ids.add(event_id)
    return item


def _now_utc() -> str:
    now = _datetime.datetime.now(_datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_unavailable_record(value: object) -> dict[str, object]:
    item = _exact(
        value,
        {"schema", "source_book", "recorded_at_utc", "reason", "detail"},
        "unavailable_record",
    )
    if item["schema"] != UNAVAILABLE_SCHEMA:
        raise ValueError("unavailable record schema differs")
    if item["source_book"] not in SUPPORTED_BOOK_IDS:
        raise ValueError("unavailable record source_book is unsupported")
    canonical_utc(item["recorded_at_utc"])
    if canonical_utc_ns(item["recorded_at_utc"]) > canonical_utc_ns(_now_utc()):
        raise ValueError("unavailable record timestamp is in the future")
    if item["reason"] not in _REASONS:
        raise ValueError("unavailable record reason is unsupported")
    if not isinstance(item["detail"], str) or not item["detail"]:
        raise ValueError("unavailable record detail must be non-empty")
    return item


def validate_capture_outcome(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("capture outcome must be an object")
    status = value.get("status")
    if status == "AVAILABLE":
        item = _exact(
            value,
            {
                "schema",
                "content_sha256",
                "status",
                "source_book",
                "raw_capture_relative",
                "raw_capture_sha256",
                "captured_at_utc",
                "event_count",
            },
            "available_outcome",
        )
        if item["schema"] != OUTCOME_SCHEMA:
            raise ValueError("available outcome schema differs")
        _sha(item["content_sha256"], "available_outcome.content_sha256")
        if item["source_book"] not in SUPPORTED_BOOK_IDS:
            raise ValueError("available outcome source_book is unsupported")
        canonical_utc(item["captured_at_utc"])
        _sha(item["raw_capture_sha256"], "available_outcome.raw_capture_sha256")
        relative = validate_relative_path(Path(str(item["raw_capture_relative"])))
        if not PurePosixPath(relative.as_posix()).is_relative_to(RAW_PREFIX):
            raise ValueError("available outcome raw path is outside raw prefix")
        if not isinstance(item["event_count"], int) or item["event_count"] <= 0:
            raise ValueError("available outcome event_count must be positive")
    elif status == "UNAVAILABLE":
        item = _exact(
            value,
            {"schema", "content_sha256", "status", "source_book", "recorded_at_utc", "reason", "detail"},
            "unavailable_outcome",
        )
        if item["schema"] != OUTCOME_SCHEMA:
            raise ValueError("unavailable outcome schema differs")
        _sha(item["content_sha256"], "unavailable_outcome.content_sha256")
        if item["source_book"] not in SUPPORTED_BOOK_IDS:
            raise ValueError("unavailable outcome source_book is unsupported")
        canonical_utc(item["recorded_at_utc"])
        if item["reason"] not in _REASONS:
            raise ValueError("unavailable outcome reason is unsupported")
        if not isinstance(item["detail"], str) or not item["detail"]:
            raise ValueError("unavailable outcome detail must be non-empty")
    else:
        raise ValueError("capture outcome status must be AVAILABLE or UNAVAILABLE")
    without = dict(item)
    without["content_sha256"] = ""
    if item["content_sha256"] != content_digest(without):
        raise ValueError("capture outcome content_sha256 differs")
    return item


def _outcome_with_digest(value: dict[str, object]) -> dict[str, object]:
    body = dict(value)
    body["content_sha256"] = ""
    body["content_sha256"] = content_digest(body)
    return body


def _read_sidecar(trusted: TrustedRoot, relative: Path, label: str) -> RegularFileSnapshot:
    snapshot = read_regular_snapshot(trusted, relative, label, max_bytes=65)
    if snapshot is None or len(snapshot.contents) != 65 or snapshot.contents[-1:] != b"\n":
        raise ValueError(f"{label} must be a 65-byte digest sidecar")
    try:
        digest = snapshot.contents[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not ASCII") from exc
    _sha(digest, label)
    return snapshot


def _read_capture(trusted: TrustedRoot, raw_capture: Path) -> tuple[RegularFileSnapshot, RegularFileSnapshot, dict[str, object]]:
    raw_relative = validate_relative_path(raw_capture)
    if not PurePosixPath(raw_relative.as_posix()).is_relative_to(RAW_PREFIX):
        raise ValueError("raw capture must be below the raw prefix")
    if raw_relative.suffix != ".json" or _SHA_RE.fullmatch(raw_relative.stem) is None:
        raise ValueError("raw capture filename must be its SHA-256")
    snapshot = read_regular_snapshot(trusted, raw_relative, "raw market capture", max_bytes=MAX_CAPTURE_BYTES)
    assert snapshot is not None
    sidecar = _read_sidecar(trusted, raw_relative.with_suffix(".sha256"), "raw market capture sidecar")
    expected_sidecar = sidecar_bytes(snapshot.contents)
    if sidecar.contents != expected_sidecar:
        raise ValueError("raw market capture sidecar digest differs")
    payload = _load_json(snapshot, "raw market capture")
    validate_raw_market_capture(payload)
    if payload["content_sha256"] != raw_relative.stem:
        raise ValueError("raw capture filename differs from content_sha256")
    return snapshot, sidecar, payload


def _write_or_reuse(trusted: TrustedRoot, relative: Path, contents: bytes, label: str) -> RegularFileSnapshot:
    existing = read_regular_snapshot(trusted, relative, label, max_bytes=max(len(contents), 65), missing_ok=True)
    if existing is not None:
        if existing.contents != contents:
            raise ValueError(f"conflicting append-only {label}")
        return existing
    return write_exclusive(trusted, relative, contents, label)


def _persist_outcome(trusted: TrustedRoot, outcome: dict[str, object]) -> Path:
    canonical = validate_capture_outcome(outcome)
    contents = canonical_json_bytes(canonical)
    relative = Path(OUTCOME_PREFIX / f"{canonical['content_sha256']}.json")
    _write_or_reuse(trusted, relative, contents, "capture outcome")
    _write_or_reuse(trusted, relative.with_suffix(".sha256"), sidecar_bytes(contents), "capture outcome sidecar")
    return relative


def admit_raw_market_capture(root: Path, raw_capture: Path) -> Path:
    with open_trusted_root(root) as trusted:
        raw_snapshot, raw_sidecar, payload = _read_capture(trusted, raw_capture)
        source_book = payload["source_book"]
        assert isinstance(source_book, str)
        outcome = _outcome_with_digest(
            {
                "schema": OUTCOME_SCHEMA,
                "content_sha256": "",
                "status": "AVAILABLE",
                "source_book": source_book,
                "raw_capture_relative": validate_relative_path(raw_capture).as_posix(),
                "raw_capture_sha256": hashlib.sha256(raw_snapshot.contents).hexdigest(),
                "captured_at_utc": payload["captured_at_utc"],
                "event_count": len(payload["events"]),
            }
        )
        result = _persist_outcome(trusted, outcome)
        trusted.assert_bound()
        rebind_regular_snapshot(trusted, raw_snapshot, "raw market capture")
        rebind_regular_snapshot(trusted, raw_sidecar, "raw market capture sidecar")
        return result


def record_unavailable_book(root: Path, unavailable_record: Path) -> Path:
    with open_trusted_root(root) as trusted:
        relative = validate_relative_path(unavailable_record)
        record_snapshot = read_regular_snapshot(trusted, relative, "unavailable book record", max_bytes=64 * 1024)
        assert record_snapshot is not None
        record = _load_json(record_snapshot, "unavailable book record")
        checked = _validate_unavailable_record(record)
        outcome = _outcome_with_digest(
            {
                "schema": OUTCOME_SCHEMA,
                "content_sha256": "",
                "status": "UNAVAILABLE",
                "source_book": checked["source_book"],
                "recorded_at_utc": checked["recorded_at_utc"],
                "reason": checked["reason"],
                "detail": checked["detail"],
            }
        )
        result = _persist_outcome(trusted, outcome)
        rebind_regular_snapshot(trusted, record_snapshot, "unavailable book record")
        return result


def load_capture_outcome(trusted: TrustedRoot, relative: Path) -> LoadedCaptureOutcome:
    relative = validate_relative_path(relative)
    if not PurePosixPath(relative.as_posix()).is_relative_to(OUTCOME_PREFIX):
        raise ValueError("capture outcome must be below the outcome prefix")
    snapshot = read_regular_snapshot(trusted, relative, "capture outcome", max_bytes=256 * 1024)
    assert snapshot is not None
    sidecar = _read_sidecar(trusted, relative.with_suffix(".sha256"), "capture outcome sidecar")
    if sidecar.contents != sidecar_bytes(snapshot.contents):
        raise ValueError("capture outcome sidecar digest differs")
    payload = validate_capture_outcome(_load_json(snapshot, "capture outcome"))
    if relative.suffix != ".json" or relative.stem != payload["content_sha256"]:
        raise ValueError("capture outcome filename differs from content_sha256")
    raw_snapshot = raw_sidecar = None
    if payload["status"] == "AVAILABLE":
        raw_relative = Path(str(payload["raw_capture_relative"]))
        raw_snapshot, raw_sidecar, raw_payload = _read_capture(trusted, raw_relative)
        if (
            raw_payload["source_book"] != payload["source_book"]
            or hashlib.sha256(raw_snapshot.contents).hexdigest()
            != payload["raw_capture_sha256"]
        ):
            raise ValueError("available outcome is not bound to its raw capture")
        if len(raw_payload["events"]) != payload["event_count"]:
            raise ValueError("available outcome event_count differs")
    return LoadedCaptureOutcome(relative, payload, snapshot, sidecar, raw_snapshot, raw_sidecar)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="admit external multibook market evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    admit = sub.add_parser("admit")
    admit.add_argument("--root", type=Path, required=True)
    admit.add_argument("--raw-capture", type=Path, required=True)
    unavailable = sub.add_parser("unavailable")
    unavailable.add_argument("--root", type=Path, required=True)
    unavailable.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = admit_raw_market_capture(args.root, args.raw_capture) if args.command == "admit" else record_unavailable_book(args.root, args.record)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 2
    print(result.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
