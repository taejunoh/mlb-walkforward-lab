"""Paired, research-only reporting for same-input temporal shadow records."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .market_play_contracts import canonical_json_bytes, canonical_utc_ns, validate_relative_path
from .prospective_ml_observation import _relative, _sha, load_ml_snapshot
from .prospective_ml_settlement import _validated_head_chain, calibration_metrics, load_official_result_evidence
from .prospective_ml_shadow import load_shadow_attachment
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


REPORT_SCHEMA = "PROSPECTIVE_ML_SHADOW_PAIRED_REPORT_V1"
REPORT_ROOT = Path("reports/prospective_ml_shadow")
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BuiltShadowPairedReport:
    root: Path
    report: dict[str, object]
    report_bytes: bytes


def _digest_body(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def _read(root: Path, path: Path, label: str) -> bytes:
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(trusted, path, label, max_bytes=MAX_BYTES)
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    return snapshot.contents


def _ref(root: Path, path: Path | str, label: str) -> dict[str, str]:
    relative = _relative(root, path, label)
    return {"relative": relative.as_posix(), "sha256": _sha(_read(root, relative, label))}


def _metric_rows(rows: Iterable[Mapping[str, object]], key: str) -> list[tuple[float, bool]]:
    result = []
    for row in rows:
        try:
            probability = float(row[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("shadow metric probability differs") from exc
        if not 0.0 <= probability <= 1.0 or type(row.get("home_won")) is not bool:
            raise ValueError("shadow metric row differs")
        result.append((probability, row["home_won"]))
    return result


def _reliability(rows: Iterable[Mapping[str, object]], key: str) -> list[dict[str, object]]:
    values = list(rows)
    result = []
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        members = [row for row in values if lower <= float(row[key]) and (float(row[key]) < upper or index == 9)]
        result.append({"lower": lower, "upper": upper, "n": len(members), "mean_probability": sum(float(row[key]) for row in members) / len(members) if members else None, "observed_home_win_rate": sum(bool(row["home_won"]) for row in members) / len(members) if members else None})
    return result


def _model_metrics(rows: Iterable[Mapping[str, object]], key: str) -> dict[str, object]:
    values = list(rows)
    metrics = calibration_metrics(_metric_rows(values, key))
    metrics["reliability_bins"] = _reliability(values, key)
    return metrics


def paired_metrics(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Compute independent model metrics over the same paired outcomes."""
    values = list(rows)
    baseline = _model_metrics(values, "baseline_p_home")
    candidate = _model_metrics(values, "candidate_p_home")
    delta = {}
    for key in ("accuracy", "brier", "log_loss"):
        delta[key] = None if baseline[key] is None or candidate[key] is None else float(candidate[key]) - float(baseline[key])
    return {"baseline": baseline, "candidate": candidate, "candidate_minus_baseline": delta}


def _profile_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    return {name: paired_metrics([row for row in rows if name == "all" or row["completeness_stratum"] == name]) for name in ("all", "completeness_lt_0_50", "completeness_ge_0_50")}


def _cohort_metrics(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        model = row.get("baseline_model_card_sha256")
        profile = row.get("profile_sha256")
        if not isinstance(model, str) or not isinstance(profile, str):
            raise ValueError("shadow cohort identity differs")
        grouped.setdefault((model, profile), []).append(dict(row))
    return [{"baseline_model_card_sha256": model, "profile_sha256": profile, "metrics": _profile_metrics(grouped[(model, profile)])} for model, profile in sorted(grouped)]


def _validate_result_identity(attachment: Mapping[str, object], record: Mapping[str, object]) -> None:
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("shadow settlement result differs")
    for result_key, attachment_key in (("game_pk", "game_pk"), ("official_date", "target_date"), ("away_team", "away_team"), ("home_team", "home_team")):
        if result.get(result_key) != attachment.get(attachment_key):
            raise ValueError("shadow settlement identity differs")


def _build_report(root: Path, attachments: Iterable[Path | str], settlement_heads: Iterable[Path | str]) -> dict[str, object]:
    attachment_paths = [_relative(root, item, "shadow attachment") for item in attachments]
    if len(set(attachment_paths)) != len(attachment_paths):
        raise ValueError("duplicate shadow attachment")
    loaded = []
    games: set[int] = set()
    for path in attachment_paths:
        attachment = load_shadow_attachment(root, path)
        game_pk = attachment["game_pk"]
        if type(game_pk) is not int or game_pk in games:
            raise ValueError("duplicate shadow game")
        games.add(game_pk)
        loaded.append((path, attachment))
    head_paths = [_relative(root, item, "settlement head") for item in settlement_heads]
    if len(set(head_paths)) != len(head_paths):
        raise ValueError("duplicate settlement head")
    heads: dict[str, tuple[Path, dict[str, object], str]] = {}
    for path in head_paths:
        record = _validated_head_chain(root, path)
        snapshot_sha = str(record["snapshot_sha256"])
        if snapshot_sha in heads:
            raise ValueError("conflicting settlement heads")
        heads[snapshot_sha] = (path, record, _sha(_read(root, path, "settlement head")))
    pairs: list[dict[str, object]] = []
    metric_rows: list[dict[str, object]] = []
    cohort_keys: set[tuple[str, str]] = set()
    used_heads: set[str] = set()
    for attachment_path, attachment in loaded:
        baseline, candidate = attachment["baseline"], attachment["candidate"]
        assert isinstance(baseline, Mapping) and isinstance(candidate, Mapping)
        snapshot_path = _relative(root, str(baseline["snapshot_relative"]), "baseline snapshot")
        snapshot = load_ml_snapshot(root, snapshot_path)
        completeness = snapshot.get("data_completeness")
        if completeness is None:
            raise ValueError("shadow completeness is missing")
        snapshot_sha = str(baseline["snapshot_sha256"])
        baseline_model = str(baseline["model_card_sha256"])
        profile_sha = str(attachment["profile"]["profile_sha256"])
        stratum = "completeness_lt_0_50" if float(completeness) < .5 else "completeness_ge_0_50"
        cohort_keys.add((baseline_model, profile_sha))
        item: dict[str, object] = {"game_pk": attachment["game_pk"], "target_date": attachment["target_date"], "away_team": attachment["away_team"], "home_team": attachment["home_team"], "attachment_relative": attachment_path.as_posix(), "attachment_sha256": _sha(_read(root, attachment_path, "shadow attachment commit")), "baseline_snapshot_sha256": snapshot_sha, "baseline_model_card_sha256": baseline_model, "baseline_p_home": float(baseline["p_home"]), "candidate_p_home": float(candidate["p_home"]), "profile_sha256": profile_sha, "commit_claimed_at_utc": attachment["commit_claimed_at_utc"], "completeness_stratum": stratum, "status": "PENDING", "reason": "settlement head is not supplied", "settlement_head_relative": None, "settlement_head_sha256": None}
        head = heads.get(snapshot_sha)
        if head is not None:
            head_path, record, head_sha = head
            used_heads.add(snapshot_sha)
            evidence = load_official_result_evidence(root, record["result_receipt_relative"])
            if canonical_utc_ns(evidence.observed_at_utc) <= canonical_utc_ns(str(attachment["commit_claimed_at_utc"])):
                raise ValueError("shadow settlement predates pair commit")
            _validate_result_identity(attachment, record)
            result = record["result"]
            assert isinstance(result, Mapping)
            item.update({"settlement_head_relative": head_path.as_posix(), "settlement_head_sha256": head_sha, "result_status": result["status"], "result_observed_at_utc": evidence.observed_at_utc})
            if result["status"] == "FINAL":
                winner = result.get("winner")
                if winner not in {attachment["away_team"], attachment["home_team"]}:
                    raise ValueError("shadow settlement winner differs")
                item["status"], item["reason"] = "PAIRED", None
                row = {"baseline_p_home": baseline["p_home"], "candidate_p_home": candidate["p_home"], "home_won": winner == attachment["home_team"], "completeness_stratum": stratum, "baseline_model_card_sha256": baseline_model, "profile_sha256": profile_sha}
                metric_rows.append(row)
            else:
                item["status"], item["reason"] = "EXCLUDED", "official result is not final"
        pairs.append(item)
    if used_heads != set(heads):
        raise ValueError("settlement head is unrelated to supplied attachment")
    cohorts = _cohort_metrics(metric_rows)
    present = {(entry["baseline_model_card_sha256"], entry["profile_sha256"]) for entry in cohorts}
    cohorts.extend({"baseline_model_card_sha256": model, "profile_sha256": profile, "metrics": _profile_metrics([])} for model, profile in sorted(cohort_keys - present))
    cohorts.sort(key=lambda entry: (entry["baseline_model_card_sha256"], entry["profile_sha256"]))
    report: dict[str, object] = {"schema": REPORT_SCHEMA, "shadow_status": "SHADOW_ONLY", "promotion": "NO_PROMOTION", "attachment_refs": [_ref(root, path, "shadow attachment commit") for path in attachment_paths], "settlement_head_refs": [_ref(root, path, "settlement head") for path in head_paths], "pairs": pairs, "primary_count": len(pairs), "paired_count": sum(item["status"] == "PAIRED" for item in pairs), "pending_count": sum(item["status"] == "PENDING" for item in pairs), "excluded_count": sum(item["status"] == "EXCLUDED" for item in pairs), "metrics": {"paired": _profile_metrics(metric_rows) if len(cohorts) <= 1 else None, "cohorts": cohorts}, "caveats": ["research-only same-input shadow comparison", "official result is reused from the baseline settlement head", "lower Brier/log loss is better; this is not evidence of profit, calibration, or promotion"]}
    report["content_sha256"] = _digest_body(report)
    return report


def build_shadow_paired_report(root: Path | str, attachments: Iterable[Path | str], settlement_heads: Iterable[Path | str]) -> BuiltShadowPairedReport:
    root = Path(root)
    report = _build_report(root, attachments, settlement_heads)
    return BuiltShadowPairedReport(root, report, canonical_json_bytes(report))


def _validate_report(value: Mapping[str, object]) -> dict[str, object]:
    required = {"schema", "content_sha256", "shadow_status", "promotion", "attachment_refs", "settlement_head_refs", "pairs", "primary_count", "paired_count", "pending_count", "excluded_count", "metrics", "caveats"}
    if set(value) != required:
        raise ValueError("shadow report fields differ")
    safe = json.loads(canonical_json_bytes(value))
    if safe["schema"] != REPORT_SCHEMA or safe["shadow_status"] != "SHADOW_ONLY" or safe["promotion"] != "NO_PROMOTION" or safe["content_sha256"] != _digest_body(safe):
        raise ValueError("shadow report digest differs")
    for key in ("attachment_refs", "settlement_head_refs"):
        if not isinstance(safe[key], list):
            raise ValueError("shadow report refs differ")
        for ref in safe[key]:
            if not isinstance(ref, Mapping) or set(ref) != {"relative", "sha256"} or not isinstance(ref["relative"], str) or not isinstance(ref["sha256"], str):
                raise ValueError("shadow report refs differ")
            validate_relative_path(Path(ref["relative"]))
    if not isinstance(safe["pairs"], list) or any(not isinstance(item, Mapping) for item in safe["pairs"]):
        raise ValueError("shadow report pairs differ")
    for key in ("primary_count", "paired_count", "pending_count", "excluded_count"):
        if type(safe[key]) is not int or safe[key] < 0:
            raise ValueError("shadow report counts differ")
    return safe


def persist_shadow_paired_report(root: Path | str, built: BuiltShadowPairedReport) -> Path:
    root = Path(root)
    if not isinstance(built, BuiltShadowPairedReport) or built.root != root:
        raise ValueError("built shadow report differs")
    report = _validate_report(built.report)
    if canonical_json_bytes(report) != built.report_bytes:
        raise ValueError("built shadow report transport differs")
    attachments = [ref["relative"] for ref in report["attachment_refs"]]
    heads = [ref["relative"] for ref in report["settlement_head_refs"]]
    rebuilt = build_shadow_paired_report(root, attachments, heads)
    if rebuilt.report_bytes != built.report_bytes:
        raise ValueError("shadow report inputs differ")
    digest = _sha(built.report_bytes)
    report_path = REPORT_ROOT / f"{digest}.json"
    markdown_path = REPORT_ROOT / f"{digest}.md"
    markdown = render_shadow_paired_markdown(report).encode("utf-8")

    def write_once(path: Path, raw: bytes, label: str) -> None:
        with open_trusted_root(root) as trusted:
            try:
                write_exclusive(trusted, path, raw, label)
            except FileExistsError:
                old = read_regular_snapshot(trusted, path, label, max_bytes=max(MAX_BYTES, len(raw)))
                if old is None or old.contents != raw:
                    raise ValueError(f"{label} differs")
                rebind_regular_snapshot(trusted, old, label, max_bytes=max(MAX_BYTES, len(raw)))

    write_once(report_path.with_suffix(".json.sha256"), f"{digest}\n".encode("ascii"), "shadow report sidecar")
    write_once(report_path, built.report_bytes, "shadow report")
    write_once(markdown_path, markdown, "shadow markdown")
    return report_path


def render_shadow_paired_markdown(report: Mapping[str, object]) -> str:
    value = _validate_report(report)
    lines = ["# Prospective ML shadow comparison", "", "Research-only; `NO_PROMOTION`.", "", f"- Primary pairs: {value['primary_count']}", f"- Paired Final: {value['paired_count']}", f"- Pending: {value['pending_count']}", f"- Excluded non-Final: {value['excluded_count']}", "", "| gamePk | status | baseline p(home) | candidate p(home) | reason |", "| ---: | --- | ---: | ---: | --- |"]
    for row in value["pairs"]:
        lines.append(f"| {row['game_pk']} | {row['status']} | {row['baseline_p_home']:.6f} | {row['candidate_p_home']:.6f} | {row.get('reason') or ''} |")
    metrics = value["metrics"]
    assert isinstance(metrics, Mapping)
    paired = metrics["paired"]
    cohorts = metrics["cohorts"]
    metric_sets: list[tuple[str, Mapping[str, object]]] = []
    if isinstance(paired, Mapping):
        metric_sets.append(("paired", paired))
    elif isinstance(cohorts, list):
        for cohort in cohorts:
            if isinstance(cohort, Mapping) and isinstance(cohort.get("metrics"), Mapping):
                metric_sets.append((f"model {cohort['baseline_model_card_sha256']} / profile {cohort['profile_sha256']}", cohort["metrics"]))
    for cohort_label, cohort_metrics in metric_sets:
        for model in ("baseline", "candidate"):
            model_metrics = cohort_metrics["all"][model]
            heading = f"{model.title()} reliability" if cohort_label == "paired" else f"{model.title()} reliability ({cohort_label})"
            lines.extend(["", f"### {heading}", "", "| probability bin | n | mean predicted home-win probability | observed home-win rate |", "| --- | ---: | ---: | ---: |"])
            for bin_value in model_metrics["reliability_bins"]:
                if bin_value["n"]:
                    lines.append(f"| [{bin_value['lower']:.1f}, {bin_value['upper']:.1f}) | {bin_value['n']} | {bin_value['mean_probability']:.6f} | {bin_value['observed_home_win_rate']:.6f} |")
            if not any(bin_value["n"] for bin_value in model_metrics["reliability_bins"]):
                lines.append("| (no paired observations) | 0 | — | — |")
    lines.append("\nBrier/log loss are probability errors; lower is better. Reliability bins compare mean predicted home-win probability with observed home-win rate. Pending and non-Final games are not losses. This report is not evidence of profit, calibration, or validated promotion.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build paired prospective ML shadow report")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--root", required=True)
    report.add_argument("--attachment", action="append", default=[])
    report.add_argument("--settlement-head", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        root = Path(args.root)
        print(persist_shadow_paired_report(root, build_shadow_paired_report(root, args.attachment, args.settlement_head)).as_posix())
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
