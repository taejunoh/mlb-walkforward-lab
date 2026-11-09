"""One-shot collection of official results for an explicit ML calibration report."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .live_sources import _persist_observation, fetch_live_http, new_observation_id
from .live_top3_contracts import LiveHttpObservation
from .prospective_ml_observation import (
    _relative,
    _sha,
    load_ml_snapshot,
    validate_primary_claim,
)
from .prospective_ml_settlement import (
    ROOT as SETTLEMENT_ROOT,
    _parse_result,
    _validated_head_chain,
    build_calibration_report,
    build_settlement,
    load_official_result_evidence,
    persist_calibration_report,
    persist_settlement,
    validate_calibration_report,
)
from .secure_fs import open_trusted_root, read_regular_snapshot


MAX_REPORT_BYTES = 64 * 1024 * 1024
_TERMINAL_STATUSES = frozenset({"FINAL", "POSTPONED", "SUSPENDED", "TIE", "NO_CONTEST"})


@dataclass(frozen=True, slots=True)
class ResultDisposition:
    game_pk: int
    snapshot_relative: str
    state: str
    receipt_relative: str | None
    head_relative: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CollectedResults:
    report_relative: Path | None
    dispositions: tuple[ResultDisposition, ...]
    report_error: str | None = None

    def as_json(self) -> str:
        return json.dumps(
            {
                "report_relative": None if self.report_relative is None else self.report_relative.as_posix(),
                "dispositions": [asdict(item) for item in self.dispositions],
                "report_error": self.report_error,
            },
            sort_keys=True,
        )


class CollectionFailure(ValueError):
    """A failed pass that may still have durable, inspectable partial output."""

    def __init__(self, message: str, result: CollectedResults | None = None) -> None:
        super().__init__(message)
        self.result = result


def _read_report(root: Path, report: Path | str) -> tuple[Path, dict[str, object]]:
    relative = _relative(root, report, "calibration report")
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(
            trusted, relative, "calibration report", max_bytes=MAX_REPORT_BYTES
        )
        sidecar = read_regular_snapshot(
            trusted,
            relative.with_suffix(relative.suffix + ".sha256"),
            "calibration report sidecar",
            max_bytes=128,
        )
    if snapshot is None or sidecar is None:
        raise ValueError("calibration report is missing")
    if sidecar.contents != f"{_sha(snapshot.contents)}\n".encode("ascii"):
        raise ValueError("calibration report sidecar differs")
    try:
        value = json.loads(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("calibration report is invalid") from exc
    value = validate_calibration_report(value)
    # Rebuild against real claims/snapshots/heads: the report's claimed counts,
    # references, and content hash are not authority by themselves.
    rebuilt = build_calibration_report(
        root, value["primary_claims"], value["settlement_heads"]
    )
    if rebuilt.report_bytes != snapshot.contents:
        raise ValueError("calibration report inputs differ")
    return relative, value


def _fixed_first_head_exists(root: Path, snapshot_sha256: str) -> bool:
    path = SETTLEMENT_ROOT / "settlement_heads" / snapshot_sha256 / "00000001.json"
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(
            trusted, path, "fixed settlement head", max_bytes=MAX_REPORT_BYTES, missing_ok=True
        )
    return snapshot is not None


def _require_sidecar(root: Path, relative: Path, label: str) -> None:
    with open_trusted_root(root) as trusted:
        content = read_regular_snapshot(trusted, relative, label, max_bytes=MAX_REPORT_BYTES)
        sidecar = read_regular_snapshot(
            trusted, relative.with_suffix(relative.suffix + ".sha256"), f"{label} sidecar", max_bytes=128
        )
    if content is None or sidecar is None or sidecar.contents != f"{_sha(content.contents)}\n".encode("ascii"):
        raise ValueError(f"{label} sidecar differs")


def _state(result: dict[str, object]) -> str:
    status = result["status"]
    if status == "FINAL":
        return "FINAL"
    if status == "PENDING":
        return "PENDING"
    if isinstance(status, str) and status.startswith("POSTPON"):
        return "POSTPONED"
    if isinstance(status, str) and status.startswith("SUSPEND"):
        return "SUSPENDED"
    if isinstance(status, str) and (status.startswith("TIE") or status.startswith("NO_CONTEST") or status.startswith("CANCEL")):
        return "TIE" if status.startswith("TIE") else "NO_CONTEST"
    return "PENDING"


def _official_url(snapshot: dict[str, object]) -> str:
    return (
        "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        f"&date={snapshot['target_date']}&gamePks={snapshot['game_pk']}"
    )


def _identity_matches(snapshot: dict[str, object], result: dict[str, object]) -> bool:
    return all(
        result[key] == snapshot[other]
        for key, other in (
            ("game_pk", "game_pk"),
            ("official_date", "target_date"),
            ("away_team", "away_team"),
            ("home_team", "home_team"),
        )
    )


def _partial_report(
    root: Path,
    claims: list[Path],
    heads: list[Path],
    dispositions: list[ResultDisposition],
) -> CollectedResults:
    try:
        report = persist_calibration_report(root, build_calibration_report(root, claims, heads))
    except Exception as exc:
        return CollectedResults(None, tuple(dispositions), str(exc))
    return CollectedResults(report, tuple(dispositions))


def collect_results(
    root: Path | str,
    report: Path | str,
    *,
    fetcher: Callable[..., LiveHttpObservation] = fetch_live_http,
    durable_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CollectedResults:
    """Fetch each explicitly unresolved primary once and make FINAL settlements."""
    root = Path(root)
    _, current = _read_report(root, report)
    claims = [Path(value) for value in current["primary_claims"]]
    heads = [Path(value) for value in current["settlement_heads"]]
    settled: set[str] = set()
    for head in heads:
        record = _validated_head_chain(root, head)
        state = _state(dict(record["result"]))
        if state not in _TERMINAL_STATUSES:
            raise ValueError("pending settlement head requires prospective_ml_settlement settle --prior")
        settled.add(str(record["snapshot_sha256"]))

    unresolved: list[tuple[Path, dict[str, object], str]] = []
    # Complete all admission checks before the first HTTP request.
    for claim_path in claims:
        claim = validate_primary_claim(root, claim_path)
        candidate = Path(str(claim["candidate_relative"]))
        _require_sidecar(root, candidate, "primary candidate")
        snapshot = load_ml_snapshot(root, candidate)
        digest = str(claim["candidate_sha256"])
        if digest in settled:
            continue
        if _fixed_first_head_exists(root, digest):
            raise ValueError("stale report omits settlement head; use the latest report/head")
        unresolved.append((candidate, snapshot, digest))

    committed_heads = list(heads)
    dispositions: list[ResultDisposition] = []
    for candidate, snapshot, _digest in unresolved:
        game_pk = int(snapshot["game_pk"])
        url = _official_url(snapshot)
        receipt_relative: str | None = None
        try:
            http = fetcher("stats_api", url)
            observation_id = new_observation_id()
            persisted = _persist_observation(
                root,
                raw_source="mlb_official_result",
                source="stats_api",
                binding_role="FINAL_SCHEDULE",
                game_pks=(game_pk,),
                observation_id=observation_id,
                query={
                    "game_pk": game_pk,
                    "kind": "prospective_ml_official_result",
                    "observation_id": observation_id,
                    "url": url,
                },
                http=http,
                durable_clock=durable_clock,
            )
            receipt = Path(persisted.source_binding.receipt_relative)
            receipt_relative = receipt.as_posix()
            evidence = load_official_result_evidence(root, receipt)
            parsed = _parse_result(evidence.raw)
            if not _identity_matches(snapshot, parsed):
                raise ValueError("official result does not bind snapshot")
            state = _state(parsed)
            if state != "FINAL":
                dispositions.append(
                    ResultDisposition(game_pk, candidate.as_posix(), state, receipt_relative, None, "official result is not final")
                )
                continue
            settlement = persist_settlement(root, build_settlement(root, candidate, receipt))
            committed_heads.append(settlement.head_path)
            dispositions.append(
                ResultDisposition(game_pk, candidate.as_posix(), "FINAL", receipt_relative, settlement.head_path.as_posix(), None)
            )
        except Exception as exc:
            dispositions.append(
                ResultDisposition(game_pk, candidate.as_posix(), "FAILED", receipt_relative, None, str(exc))
            )
            partial = _partial_report(root, claims, committed_heads, dispositions)
            raise CollectionFailure(str(exc), partial) from exc

    try:
        next_report = persist_calibration_report(
            root, build_calibration_report(root, claims, committed_heads)
        )
    except Exception as exc:
        raise CollectionFailure(
            str(exc), CollectedResults(None, tuple(dispositions), str(exc))
        ) from exc
    return CollectedResults(next_report, tuple(dispositions))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect official MLB result evidence once")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--root", required=True)
    collect.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        result = collect_results(Path(args.root), args.report)
    except CollectionFailure as exc:
        output: dict[str, object] = {"error": str(exc)}
        if exc.result is not None:
            output["partial"] = json.loads(exc.result.as_json())
        print(json.dumps(output, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1
    print(result.as_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
