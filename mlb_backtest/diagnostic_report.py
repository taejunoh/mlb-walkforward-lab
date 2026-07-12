from collections.abc import Mapping, Sequence
from pathlib import Path
import argparse

from mlb_backtest.diagnostics import (
    build_diagnostic,
    canonical_json_bytes,
    load_v51_evidence,
    sha256_file,
)


def iter_contract_tables(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    tables = payload.get("tables")
    if not isinstance(tables, Mapping):
        raise ValueError("diagnostic tables registry missing")
    ordered = tuple(tables[name] for name in sorted(tables))
    if not all(isinstance(table, Mapping) for table in ordered):
        raise ValueError("diagnostic table must be a mapping")
    return ordered  # type: ignore[return-value]


def write_diagnostic_reports(root: Path, output_dir: Path) -> dict[str, str]:
    payload = build_diagnostic(load_v51_evidence(root))
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "diagnostic_100.json"
    md_path = output_dir / "diagnostic_100.md"
    json_path.write_bytes(canonical_json_bytes(payload) + b"\n")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {
        "json_sha256": sha256_file(json_path),
        "markdown_sha256": sha256_file(md_path),
    }


def markdown_table(
    headers: Sequence[str], rows: Sequence[Sequence[object]]
) -> str:
    def escape(value: object) -> str:
        return str(value).replace("|", "\\|")

    return "\n".join(
        [
            "| " + " | ".join(map(escape, headers)) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(map(escape, row)) + " |" for row in rows),
        ]
    )


def render_markdown(d: Mapping[str, object]) -> str:
    primary = d["primary"]
    baselines_data = d["baselines"]
    calibration_data = d["calibration"]
    discrimination = d["discrimination"]
    rank_data = d["rank"]
    rank_comparisons = d["rank_comparisons"]
    monthly_data = d["monthly"]
    components_data = d["components"]
    home = baselines_data["home"]
    season = baselines_data["season_record"]
    record = primary["record"]
    home_paired = home["paired"]
    season_paired = season["paired"]

    calibration = markdown_table(
        ["구간", "N", "평균확률", "실제적중", "gap", "Brier 기여"],
        [
            [
                row["bin"],
                row["N"],
                "—"
                if not row["N"]
                else f'{row["mean_probability"]:.6f}',
                "—" if not row["N"] else f'{row["actual_accuracy"]:.6f}',
                "—" if not row["N"] else f'{row["gap"]:.6f}',
                f'{row["brier_contribution"]:.10f}',
            ]
            for row in calibration_data["bins"]
        ],
    )
    components = markdown_table(
        ["선택측 edge", "승리 평균", "패배 평균"],
        [
            [
                name,
                f'{components_data[name]["win_mean"]:.3f}',
                f'{components_data[name]["loss_mean"]:.3f}',
            ]
            for name in ("starter", "offense", "bullpen", "schedule")
        ],
    )
    baselines = markdown_table(
        ["비교", "N", "모델", "기준선", "모델만", "기준선만", "McNemar p"],
        [
            [
                "홈팀",
                home_paired["N"],
                f'{round(home["model_accuracy"] * home_paired["N"])}/{home_paired["N"]}',
                f'{round(home["baseline_accuracy"] * home_paired["N"])}/{home_paired["N"]}',
                home_paired["model_only"],
                home_paired["baseline_only"],
                f'{home["mcnemar_p"]:.4f}',
            ],
            [
                "D-1 시즌승률",
                season_paired["N"],
                f'{round(season["model_accuracy"] * season_paired["N"])}/{season_paired["N"]}',
                f'{round(season["baseline_accuracy"] * season_paired["N"])}/{season_paired["N"]}',
                season_paired["model_only"],
                season_paired["baseline_only"],
                f'{season["mcnemar_p"]:.4f}',
            ],
        ],
    )
    rank_by_number = {row["rank"]: row for row in rank_data}
    comparison_by_rank = {row["rank1_vs"]: row for row in rank_comparisons}
    monthly_records = [monthly_data[key]["record"] for key in sorted(monthly_data)]
    wilson_low, wilson_high = primary["wilson_95"]
    probability_spearman = discrimination["probability_spearman"]
    bullpen = components_data["bullpen"]
    halves = bullpen["absolute_edge_halves"]
    bullpen_spearman = bullpen["spearman"]

    sections = [
        "# MLB v5.1 100픽 진단",
        "## Executive Summary\n\n"
        f'{record["W"]}승 {record["L"]}패는 고무적이지만 시즌 승률 기준선과 '
        f'다른 {season_paired["model_only"] + season_paired["baseline_only"]}경기는 '
        f'{season_paired["model_only"]} 대 {season_paired["baseline_only"]}에 그쳤다. '
        "추가 예측 우위는 입증되지 않았다. 재구성된 최종 참가자는 실제 T-45 "
        "재현성을 입증하지 않는다.",
        "## 지표·모집단·분모 정의\n\n"
        f'모집단은 {d["population"]["dates"][0]}~{d["population"]["dates"][1]}의 '
        f'봉인된 Daily Top 1 {d["population"]["N"]}경기다. '
        "Accuracy=W/N; Brier=mean((p-y)^2); Log loss=-mean(y log p+(1-y)log(1-p)); "
        "ECE는 고정 5개 구간의 N가중 절대 gap이다. 짝지은 delta는 양쪽 예측이 모두 "
        f'정의된 경기만 분모로 쓰며 시즌승률 tie 1건을 제외해 N={season_paired["N"]}, '
        f'시장 비교는 N={baselines_data["market"]["N"]}이다.',
        "## 1차 결과\n\n"
        f'{record["W"]}승 {record["L"]}패, N={record["N"]}, '
        f'적중률 {100 * primary["accuracy"]:.2f}%, Wilson 95% '
        f'{100 * wilson_low:.4f}%–{100 * wilson_high:.4f}%. '
        f'Brier {primary["brier"]:.10f}, Log loss {primary["log_loss"]:.10f}, '
        f'ECE {calibration_data["ece"]:.10f}, 최대 연패 {primary["max_losing_streak"]}.',
        "## 짝지은 기준선\n\n"
        + baselines
        + f'\n\n시장 N={baselines_data["market"]["N"]}이므로 시장 우위나 베팅 성과를 평가하지 않는다.',
        "## 확률 보정과 판별\n\n"
        + calibration
        + f'\n\n확률–결과 Spearman ρ={probability_spearman["rho"]:.4f}, '
        f'p={probability_spearman["p"]:.3f}. ≥63%와 ≥68%는 사후 진단이며 새 컷이 아니다.',
        "## 일별 순위\n\n"
        f'Rank 1/2/3은 {rank_by_number[1]["W"]}/{rank_by_number[1]["N"]}, '
        f'{rank_by_number[2]["W"]}/{rank_by_number[2]["N"]}, '
        f'{rank_by_number[3]["W"]}/{rank_by_number[3]["N"]}이고 Rank 1 대 2/3 exact '
        f'McNemar p는 {comparison_by_rank[2]["p"]:.3f}/{comparison_by_rank[3]["p"]:.3f}이다.',
        "## 시간·팀 집중\n\n"
        + ", ".join(
            f'{index + 5}월 {item["W"]}/{item["N"]}'
            for index, item in enumerate(monthly_records)
        )
        + f'다. 7월 대 나머지 Fisher p={d["temporal"]["july_vs_other_fisher_p"]:.4f}는 '
        f'불안정성 증거이지 체제 발견이 아니다. {d["team_concentration"]["distinct_teams"]}개 팀 중 '
        f'상위 6개가 {100 * d["team_concentration"]["top_six_share"]:.0f}%이며 팀별 Wilson 구간은 '
        "작은 셀의 불확실성을 보인다.",
        "## 컴포넌트\n\n"
        + components
        + f'\n\n불펜 절대 edge 하/상위 절반은 '
        f'{100 * halves["lower"]["accuracy"]:.0f}%/{100 * halves["upper"]["accuracy"]:.0f}%, '
        f'signed 불펜 edge–결과 ρ={bullpen_spearman["rho"]:.3f}, '
        f'p={bullpen_spearman["p"]:.3f}다. 이는 prior-season 가설이지 재가중 근거가 아니다.',
        "## 데이터 품질·누수\n\n"
        "Top 1 전부 INCOMPLETE/High/Toss-up이고 수비·환경은 50, quality 0이다. "
        "D-1/target-game 배제와 잠금 행의 outcome 필드 부재·평가 예측 필드 결합은 "
        "확인됐다. 그러나 outcome-join 실행 타임스탬프가 없다. 따라서 v5.1 100픽 진단의 "
        "통계 as-of 집계 상태는 `LIMITATION`이고 잠금→결과 결합 순서는 "
        "`NOT_TIMESTAMP_PROVEN`이다. 참가자는 사후 최종 기록이고 공급자 수정 상태도 과거 DB "
        "스냅샷이 아니다. 7,265경기 동일 pool의 α/β는 rolling-origin OOF가 아니다.",
        "## 권고\n\nv5.2-QD는 예측 구조를 유지하고 데이터 완전성 의미와 OOF 보정만 "
        "동결한다. 역사 500과 실제 T-45 prospective ledger를 분리한다.",
        "## 추가 질문\n\n2018–2021 원천을 동일 정의로 복구할 수 있는가? 참가자 feed가 T-45 "
        "freshness 계약을 충족하는가?",
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
