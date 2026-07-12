from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
import argparse
import math
import os
import tempfile

from mlb_backtest.diagnostics import (
    build_diagnostic,
    canonical_json_bytes,
    load_v51_evidence,
    sha256_file,
)


TABLE_DEFINITIONS = MappingProxyType(
    {
        "paired_baselines": "N=sum of four paired cells; model/baseline wins come from exact paired cells; delta=model accuracy minus baseline accuracy; interval uses z=1.97.",
        "calibration": "N=bin count; gap=absolute mean-probability minus actual-accuracy difference; Brier contribution is weighted over all Top 1 picks.",
        "rank": "W=correct predictions and N=graded predictions at immutable daily_rank.",
        "monthly": "Month is the actual YYYY-MM official_date key; W/L/N and accuracy use graded Top 1 picks.",
        "teams": "Team is predicted winner; W/L/N, accuracy and Wilson 95% interval are descriptive small-cell results.",
        "components": "Edges are signed for the chosen side; win/loss means split on the observed Top 1 result.",
    }
)


def iter_contract_tables(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    tables = payload.get("tables")
    if not isinstance(tables, Mapping):
        raise ValueError("diagnostic tables registry missing")
    result: list[Mapping[str, object]] = []
    for name in sorted(tables):
        table = tables[name]
        if not isinstance(table, Mapping):
            raise ValueError("diagnostic table must be a mapping")
        result.append(MappingProxyType(dict(table)))
    return tuple(result)


def _write_fsynced_temp(path: Path, contents: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _publish_pair(
    json_path: Path, json_contents: bytes, md_path: Path, md_contents: bytes
) -> None:
    temporary_paths: list[Path] = []
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        temporary_paths.append(_write_fsynced_temp(json_path, json_contents))
        temporary_paths.append(_write_fsynced_temp(md_path, md_contents))
        for final_path in (json_path, md_path):
            backups[final_path] = (
                _write_fsynced_temp(final_path, final_path.read_bytes())
                if final_path.exists()
                else None
            )
        for temporary_path, final_path in zip(
            temporary_paths, (json_path, md_path), strict=True
        ):
            os.replace(temporary_path, final_path)
            replaced.append(final_path)
        _fsync_directory(json_path.parent)
    except BaseException:
        rollback_error: BaseException | None = None
        for final_path in reversed(replaced):
            backup_path = backups[final_path]
            try:
                if backup_path is None:
                    final_path.unlink(missing_ok=True)
                else:
                    os.replace(backup_path, final_path)
            except BaseException as error:
                rollback_error = rollback_error or error
        try:
            _fsync_directory(json_path.parent)
        except BaseException as error:
            rollback_error = rollback_error or error
        if rollback_error is not None:
            raise RuntimeError("diagnostic pair rollback failed") from rollback_error
        raise
    finally:
        for temporary_path in (*temporary_paths, *backups.values()):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def write_diagnostic_reports(root: Path, output_dir: Path) -> dict[str, str]:
    payload = build_diagnostic(load_v51_evidence(root))
    json_contents = canonical_json_bytes(payload) + b"\n"
    md_contents = render_markdown(payload).encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "diagnostic_100.json"
    md_path = output_dir / "diagnostic_100.md"
    _publish_pair(json_path, json_contents, md_path, md_contents)
    return {
        "json_sha256": sha256_file(json_path),
        "markdown_sha256": sha256_file(md_path),
    }


def markdown_table(
    headers: Sequence[str], rows: Sequence[Sequence[object]]
) -> str:
    def escape(value: object) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\r\n", "<br>")
            .replace("\r", "<br>")
            .replace("\n", "<br>")
            .replace("|", "\\|")
        )

    return "\n".join(
        [
            "| " + " | ".join(map(escape, headers)) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(map(escape, row)) + " |" for row in rows),
        ]
    )


def _table_contract(
    payload: Mapping[str, object], name: str, rendered_table: str
) -> str:
    table = payload["tables"][name]
    source_paths = {
        source["id"]: source["path"] for source in payload["sources"]
    }
    exclusions = table["exclusions"]
    exclusion_text = "none" if not exclusions else "; ".join(exclusions)
    sources = "; ".join(source_paths[source_id] for source_id in table["source_ids"])
    return (
        f'### Table contract: `{name}`\n\n'
        f'- Population: `{table["population"]}`\n'
        f'- Formula: `{table["formula"]}`\n'
        f'- Definitions: {TABLE_DEFINITIONS[name]}\n'
        f'- Exclusions: `{exclusion_text}`\n'
        f'- Source paths: `{sources}`\n\n'
        f"{rendered_table}"
    )


def _paired_values(name: str, baseline: Mapping[str, object]) -> dict[str, object]:
    paired = baseline["paired"]
    cells = tuple(
        paired[key]
        for key in ("both_correct", "model_only", "baseline_only", "both_wrong")
    )
    if not all(type(value) is int and value >= 0 for value in cells):
        raise ValueError(f"{name} paired cells must be nonnegative integers")
    both_correct, model_only, baseline_only, both_wrong = cells
    n = paired["N"]
    if type(n) is not int or n != sum(cells):
        raise ValueError(f"{name} paired N is incoherent")
    model_wins = both_correct + model_only
    baseline_wins = both_correct + baseline_only
    if not math.isclose(baseline["model_accuracy"], model_wins / n, abs_tol=1e-15):
        raise ValueError(f"{name} paired model accuracy is incoherent")
    if not math.isclose(
        baseline["baseline_accuracy"], baseline_wins / n, abs_tol=1e-15
    ):
        raise ValueError(f"{name} paired baseline accuracy is incoherent")
    expected_delta = (model_wins - baseline_wins) / n
    if not math.isclose(baseline["delta"], expected_delta, abs_tol=1e-15):
        raise ValueError(f"{name} paired delta is incoherent")
    if baseline["interval_label"] != "approximate paired 95% interval (z=1.97)":
        raise ValueError(f"{name} paired interval definition is incoherent")
    return {
        "name": name,
        "N": n,
        "model_wins": model_wins,
        "baseline_wins": baseline_wins,
        "both_correct": both_correct,
        "model_only": model_only,
        "baseline_only": baseline_only,
        "both_wrong": both_wrong,
        "delta": baseline["delta"],
        "interval": tuple(baseline["paired_delta_interval"]),
        "mcnemar_p": baseline["mcnemar_p"],
    }


def _percent(value: float, places: int = 4) -> str:
    return f"{100 * value:.{places}f}%"


def render_markdown(d: Mapping[str, object]) -> str:
    primary = d["primary"]
    record = primary["record"]
    baselines = d["baselines"]
    home = _paired_values("home", baselines["home"])
    season = _paired_values("D-1 season record", baselines["season_record"])
    calibration = d["calibration"]
    discrimination = d["discrimination"]
    thresholds = d["thresholds"]
    comparisons = {row["rank1_vs"]: row for row in d["rank_comparisons"]}
    components = d["components"]
    bullpen = components["bullpen"]
    quality = d["data_quality"]

    baseline_table = markdown_table(
        [
            "baseline",
            "N",
            "model",
            "baseline",
            "both correct",
            "model only",
            "baseline only",
            "both wrong",
            "delta",
            "paired 95% interval",
            "McNemar p",
        ],
        [
            [
                item["name"],
                item["N"],
                f'{item["model_wins"]}/{item["N"]}',
                f'{item["baseline_wins"]}/{item["N"]}',
                item["both_correct"],
                item["model_only"],
                item["baseline_only"],
                item["both_wrong"],
                f'{100 * item["delta"]:+.4f}pp' if item is season else f'{100 * item["delta"]:+.2f}pp',
                f'{100 * item["interval"][0]:+.2f}pp–{100 * item["interval"][1]:+.2f}pp',
                f'{item["mcnemar_p"]:.4f}',
            ]
            for item in (home, season)
        ],
    )
    calibration_table = markdown_table(
        ["bin", "N", "mean probability", "actual accuracy", "gap", "Brier contribution"],
        [
            [
                row["bin"],
                row["N"],
                "—" if not row["N"] else f'{row["mean_probability"]:.6f}',
                "—" if not row["N"] else f'{row["actual_accuracy"]:.6f}',
                "—" if not row["N"] else f'{row["gap"]:.6f}',
                f'{row["brier_contribution"]:.10f}',
            ]
            for row in calibration["bins"]
        ],
    )
    rank_table = markdown_table(
        ["rank", "W", "N", "accuracy"],
        [
            [row["rank"], row["W"], row["N"], _percent(row["W"] / row["N"])]
            for row in d["rank"]
        ],
    )
    monthly_table = markdown_table(
        ["month", "W", "L", "N", "accuracy"],
        [
            [
                month,
                values["record"]["W"],
                values["record"]["L"],
                values["record"]["N"],
                _percent(values["accuracy"]),
            ]
            for month, values in sorted(d["monthly"].items())
        ],
    )
    team_table = markdown_table(
        ["team", "N", "W", "accuracy", "Wilson 95%"],
        [
            [
                team,
                values["N"],
                values["W"],
                _percent(values["accuracy"]),
                f'{_percent(values["wilson_95"][0])}–{_percent(values["wilson_95"][1])}',
            ]
            for team, values in sorted(d["teams"].items())
        ],
    )
    component_table = markdown_table(
        ["chosen-side edge", "win mean", "loss mean"],
        [
            [name, f'{components[name]["win_mean"]:.3f}', f'{components[name]["loss_mean"]:.3f}']
            for name in ("starter", "offense", "bullpen", "schedule")
        ],
    )
    source_hashes = "\n".join(
        f'- `{source["path"]}`: `{source["sha256"]}`' for source in d["sources"]
    )
    top_six = ", ".join(
        f"{team} {count}" for team, count in d["team_concentration"]["top_six"].items()
    )
    sides = d["sides"]
    legacy = discrimination["legacy_confidence_spearman"]
    probability = discrimination["probability_spearman"]
    completeness = quality["completeness"]
    wilson_low, wilson_high = primary["wilson_95"]
    halves = bullpen["absolute_edge_halves"]
    bullpen_spearman = bullpen["spearman"]

    sections = [
        "# MLB v5.1 100픽 진단",
        "## Executive Summary\n\n"
        f'{record["W"]}승 {record["L"]}패는 고무적이지만 시즌 승률 기준선과 다른 '
        f'{season["model_only"] + season["baseline_only"]}경기는 '
        f'{season["model_only"]} 대 {season["baseline_only"]}에 그쳤다. '
        "추가 예측 우위는 입증되지 않았다. 재구성된 최종 참가자는 실제 T-45 "
        "재현성을 입증하지 않는다.",
        "## 지표·모집단·분모 정의\n\n"
        f'모집단은 {d["population"]["dates"][0]}~{d["population"]["dates"][1]}의 '
        f'봉인된 Daily Top 1 {d["population"]["N"]}경기다. '
        "Accuracy=W/N; Brier=mean((p-y)^2); Log loss=-mean(y log p+(1-y)log(1-p)); "
        "ECE는 고정 5개 구간의 N가중 절대 gap이다. 모든 계산은 봉인된 JSON의 "
        "unrounded values를 쓴다.",
        "## 1차 결과\n\n"
        f'{record["W"]}승 {record["L"]}패, N={record["N"]}, '
        f'적중률 {_percent(primary["accuracy"], 2)}, Wilson 95% '
        f'{_percent(wilson_low)}–{_percent(wilson_high)}. Brier {primary["brier"]:.10f}, '
        f'Log loss {primary["log_loss"]:.10f}, ECE {calibration["ece"]:.10f}, '
        f'최대 연패 {primary["max_losing_streak"]}.',
        "## 짝지은 기준선\n\n"
        + _table_contract(d, "paired_baselines", baseline_table)
        + "\n\nThe full paired cells are: "
        + f'home both correct {home["both_correct"]}, model only {home["model_only"]}, '
        f'baseline only {home["baseline_only"]}, both wrong {home["both_wrong"]}; '
        f'D-1 both correct {season["both_correct"]}, model only {season["model_only"]}, '
        f'baseline only {season["baseline_only"]}, both wrong {season["both_wrong"]}. '
        "Intervals use z=1.97. 시즌승률 tie 1건은 분모에서 제외했다. "
        f'시장 N={baselines["market"]["N"]}이므로 시장 우위나 베팅 성과를 평가하지 않는다.',
        "## 확률 보정과 판별\n\n"
        + _table_contract(d, "calibration", calibration_table)
        + f'\n\nProbability–outcome Spearman ρ={probability["rho"]:.4f}, p={probability["p"]:.3f}; '
        f'confidence–outcome Spearman ρ={legacy["rho"]:.4f}, p={legacy["p"]:.3f}. '
        f'≥63%: {thresholds["ge_63"]["W"]}/{thresholds["ge_63"]["N"]}='
        f'{_percent(thresholds["ge_63"]["accuracy"])}; '
        f'≥68%: {thresholds["ge_68"]["W"]}/{thresholds["ge_68"]["N"]}='
        f'{_percent(thresholds["ge_68"]["accuracy"])}. 이 결과는 post-hoc이며 '
        "monotonic calibration is not validated; 새 컷이 아니고 이 100건으로 bin과 threshold를 "
        "최적화하지 않는다.",
        "## 일별 순위\n\n"
        + _table_contract(d, "rank", rank_table)
        + f'\n\nRank 1 대 2/3 exact McNemar p는 {comparisons[2]["p"]:.3f}/'
        f'{comparisons[3]["p"]:.3f}이다. 순서는 방향성으로 유리하지만 우월성은 입증되지 않았다.',
        "## 시간·팀 집중\n\n"
        + _table_contract(d, "monthly", monthly_table)
        + f'\n\n7월 대 나머지 Fisher p={d["temporal"]["july_vs_other_fisher_p"]:.4f}는 '
        "불안정성 증거이지 체제 발견이 아니다. "
        f'home {sides["home"]["W"]}/{sides["home"]["N"]}={_percent(sides["home"]["accuracy"])}; '
        f'away {sides["away"]["W"]}/{sides["away"]["N"]}={_percent(sides["away"]["accuracy"])}. '
        f'{d["team_concentration"]["distinct_teams"]}개 팀, 상위 6개: {top_six}; '
        f'합계 {100 * d["team_concentration"]["top_six_share"]:.0f}%다. '
        "작은 셀을 지속적 팀 효과로 해석하지 않는다.\n\n"
        + _table_contract(d, "teams", team_table),
        "## 컴포넌트\n\n"
        + _table_contract(d, "components", component_table)
        + f'\n\n불펜 절대 edge 하/상위 절반은 '
        f'{_percent(halves["lower"]["accuracy"], 0)}/{_percent(halves["upper"]["accuracy"], 0)}, '
        f'signed 불펜 edge–결과 ρ={bullpen_spearman["rho"]:.3f}, '
        f'p={bullpen_spearman["p"]:.3f}다. prior-season 가설일 뿐이며 가중치·shrinkage·부호·'
        "threshold를 바꾸지 않는다.",
        "## 데이터 품질·누수\n\n"
        f'전체 {quality["all_candidate_states"]["N"]:,}개 잠금 후보와 Top 1 전부 '
        "INCOMPLETE/High/Toss-up이고 수비·환경은 50, quality 0이다. "
        f'Top 1 completeness는 {completeness["min"]:.10f}–{completeness["max"]:.10f}, '
        f'mean {completeness["mean"]:.10f}다. Lineup은 RECONSTRUCTED_FINAL_LINEUP, starter는 '
        "RECONSTRUCTED_ACTUAL_STARTER로 실제 T-45 snapshot이 아니다. "
        f'{quality["training_eligible_rows"]:,}경기 동일 pool의 α/β는 rolling-origin OOF가 아니다. '
        "Feature isolation is PASS: max_history_date <= D-1 and target gamePk Statcast excluded. "
        "Locked-row isolation is PASS: locked prediction rows contain no outcome fields and "
        "bind exactly to later evaluated prediction fields. "
        "그러나 outcome-join 실행 타임스탬프가 없다. 통계 as-of 집계 상태는 "
        "`LIMITATION`이고 잠금→결과 결합 순서는 `NOT_TIMESTAMP_PROVEN`이다. "
        "The proven PASS subchecks do not establish outcome-join chronology and do not cure "
        "participant-time or source-revision limitations. 참가자는 사후 최종 기록이고 "
        "retrieval hash는 재현성을 주지만 과거 DB snapshot은 아니다.",
        "## Source hashes\n\n" + source_hashes,
        "## 권고\n\nv5.2-QD는 예측 구조를 유지하고 데이터 완전성 의미와 OOF 보정만 "
        "동결한다. 역사 500과 실제 T-45 prospective ledger를 분리한다.",
        "## 한계와 가정\n\n독립 전향 검증, 시장 우위, ROI, CLV, 수익성, actionable betting 또는 "
        "v5.2 예측력 개선을 주장하지 않는다.",
    ]
    return "\n\n".join(sections) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    write_diagnostic_reports(args.root.resolve(), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
