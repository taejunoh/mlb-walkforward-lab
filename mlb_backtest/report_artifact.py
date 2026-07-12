from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlb_backtest.diagnostics import canonical_json_bytes


REPORT_SOURCE: dict[str, object] = {
    "id": "diagnostic-json",
    "label": "Canonical v5.1 diagnostic",
    "path": "reports/v5_1_bt/diagnostic_100.json",
    "query": {
        "engine": "python-stdlib-scipy",
        "id": "v5.1-BT-diagnostic-100",
        "description": (
            "Sealed Daily Top 1 diagnostic derived from audited v5.1 files; "
            "the SQL expression identifies the bounded canonical JSON input, "
            "which is reshaped by mlb_backtest.report_artifact."
        ),
        # The installed validator requires structured artifact sources to expose
        # runnable SQL (or a .sql source path). DuckDB can read this bounded,
        # repository-relative canonical input directly.
        "sql": (
            "SELECT *\n"
            "FROM read_json_auto('reports/v5_1_bt/diagnostic_100.json')"
        ),
        "language": "python",
        "executed_at": None,
        "tables_used": [
            "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
            "data/v5_1_bt/outcomes/baseline.json",
        ],
        "filters": [
            "top1=Yes",
            "outcome.status=Final",
            "official_date 2025-05-01 through 2025-08-12",
            "season-record tie excluded only from paired season denominator",
        ],
        "metric_definitions": [
            "Accuracy=W/N",
            "Brier=mean((pick_probability-correct)^2)",
            "Log loss=-mean(y log p+(1-y) log(1-p))",
            "ECE=sum_bin(N_bin/N)*abs(mean_probability-accuracy)",
            (
                "chosen-side edge=(home-away edge)*(+1 home prediction; "
                "-1 away prediction)"
            ),
        ],
    },
}


def markdown_block(
    block_id: str, body: str, *, sourced: bool = False
) -> dict[str, object]:
    block: dict[str, object] = {"id": block_id, "type": "markdown", "body": body}
    if sourced:
        block["sourceId"] = "diagnostic-json"
    return block


def baseline_pair_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    baselines = diagnostic["baselines"]
    assert isinstance(baselines, Mapping)
    rows: list[dict[str, object]] = []
    for label, key in (("홈팀", "home"), ("D-1 시즌 승률", "season_record")):
        value = baselines[key]
        assert isinstance(value, Mapping)
        paired = value["paired"]
        assert isinstance(paired, Mapping)
        rows.append(
            {
                "comparison": label,
                "model_accuracy": value["model_accuracy"],
                "baseline_accuracy": value["baseline_accuracy"],
                "N": paired["N"],
                "model_only": paired["model_only"],
                "baseline_only": paired["baseline_only"],
                "delta": value["delta"],
                "mcnemar_p": value["mcnemar_p"],
            }
        )
    return rows


def month_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    monthly = diagnostic["monthly"]
    assert isinstance(monthly, Mapping)
    rows: list[dict[str, object]] = []
    for month, value in sorted(monthly.items()):
        assert isinstance(value, Mapping)
        record = value["record"]
        assert isinstance(record, Mapping)
        rows.append(
            {
                "month": month,
                "accuracy": value["accuracy"],
                "W": record["W"],
                "L": record["L"],
                "N": record["N"],
            }
        )
    return rows


def component_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    components = diagnostic["components"]
    assert isinstance(components, Mapping)
    rows: list[dict[str, object]] = []
    for name in ("starter", "offense", "bullpen", "schedule"):
        value = components[name]
        assert isinstance(value, Mapping)
        rows.append(
            {
                "component": name,
                "wins_mean": value["win_mean"],
                "losses_mean": value["loss_mean"],
                "wins_N": 67,
                "losses_N": 33,
                "unit": "score points",
            }
        )
    return rows


def paired_table_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    baselines = diagnostic["baselines"]
    assert isinstance(baselines, Mapping)
    rows: list[dict[str, object]] = []
    for label, key in (("홈팀", "home"), ("D-1 시즌 승률", "season_record")):
        value = baselines[key]
        assert isinstance(value, Mapping)
        paired = value["paired"]
        assert isinstance(paired, Mapping)
        interval = value["paired_delta_interval"]
        assert isinstance(interval, list)
        rows.append(
            {
                "comparison": label,
                **paired,
                "delta": value["delta"],
                "interval_low": interval[0],
                "interval_high": interval[1],
                "mcnemar_p": value["mcnemar_p"],
            }
        )
    return rows


def team_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    teams = diagnostic["teams"]
    assert isinstance(teams, Mapping)
    rows: list[dict[str, object]] = []
    for team, values in sorted(
        teams.items(), key=lambda item: (-item[1]["N"], item[0])
    ):
        assert isinstance(values, Mapping)
        interval = values["wilson_95"]
        assert isinstance(interval, list)
        rows.append(
            {
                "team": team,
                "W": values["W"],
                "L": values["L"],
                "N": values["N"],
                "accuracy": values["accuracy"],
                "wilson_low": interval[0],
                "wilson_high": interval[1],
            }
        )
    return rows


def executive_summary_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## Executive Summary\n\n"
        "- **67%는 가능성 신호입니다.** 100픽에서 67승 33패였습니다.\n"
        "- **추가 우위는 입증되지 않았습니다.** 시즌 승률 기준선과 다른 "
        "7경기에서 4 대 3이었습니다.\n"
        "- **확률과 재현성은 미검증입니다.** 비단조 보정과 사후 참가자 "
        "재구성 한계가 남습니다."
    )


def definitions_body() -> str:
    return (
        "## 지표·모집단·분모 정의\n\n"
        "모집단은 봉인된 Daily Top 1 100경기입니다. Accuracy=W/N, "
        "Brier=mean((p-y)^2), Log loss=-mean(y log p+(1-y)log(1-p)), "
        "ECE는 고정 구간 N가중 절대 gap입니다. 홈 비교 N=100, 시즌 승률 "
        "비교는 tie 1건 제외 N=99, 시장 비교 N=0입니다."
    )


def baseline_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## 67% 대부분은 강팀 선택과 겹칩니다\n\n"
        "홈 기준선 대비 차이는 +8.00%p이지만 p=0.1338이고, D-1 시즌 승률 "
        "기준선 대비 차이는 +1.0101%p이며 p=1.0입니다. 따라서 독립적인 "
        "추가 예측 가치는 확립되지 않았습니다."
    )


def calibration_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## 높은 확률이 더 높은 적중률로 이어지지 않았습니다\n\n"
        "63-68 구간은 45/59였지만 68%+ 구간은 14/23이었습니다. 이 "
        "100픽으로 구간이나 컷을 다시 최적화하지 않습니다."
    )


def stability_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## 월별 변동과 팀 집중도가 큽니다\n\n"
        "7월은 14/27이었고 상위 6개 선택 팀이 전체 픽의 57%였습니다. "
        "이는 불안정성 진단이지 월/팀 효과의 증거가 아닙니다."
    )


def component_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## 컴포넌트 차이는 다음 검증의 가설입니다\n\n"
        "불펜과 일정 엣지는 승패 그룹에서 예상과 다른 방향을 보였습니다. "
        "이 결과로 가중치·부호·임계값을 바꾸지 않고 과거 시즌에서 사전 "
        "선언대로 검사합니다."
    )


def quality_leakage_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## 데이터 품질은 실전 재현성을 제한합니다\n\n"
        "모든 Top 1은 INCOMPLETE·High·Toss-up이고, 참가자는 최종 라인업/실제 "
        "선발로 재구성됐습니다. D-1 특성 격리와 잠금 행의 결과 필드 부재는 "
        "확인됐지만, 결과 결합 실행 시각이 없어 잠금 후 결합 순서는 입증되지 "
        "않았습니다(`NOT_TIMESTAMP_PROVEN`). 통계 as-of 집계 상태는 "
        "`LIMITATION`이며 참가자 시점과 공급자 수정 이력도 별도 한계로 남습니다."
    )


def next_steps_body() -> str:
    return (
        "## Recommended Next Steps\n\n"
        "1. v5.2-QD 규칙과 해시를 동결합니다.\n"
        "2. 2018-2021 필수 소스를 확보한 뒤 H22/H23/H24를 적합합니다.\n"
        "3. 역사 500과 전향 T-45 장부를 절대 합치지 않습니다."
    )


def further_questions_body() -> str:
    return (
        "## Further Questions\n\n"
        "- 사전 시점 참가자 데이터가 재구성 참가자 결과를 재현합니까?\n"
        "- 불펜/일정 신호는 2022-2024 워크포워드에서도 같은 방향입니까?"
    )


def caveats_body(diagnostic: Mapping[str, object]) -> str:
    del diagnostic
    return (
        "## Caveats and Assumptions\n\n"
        "시장 N=0이므로 시장 엣지나 베팅 성과를 평가하지 않습니다. 2025는 "
        "개발 진단 표본이며 향후 역사 500은 강건성 시험이지 전향 확인이 아닙니다."
    )


def exact_chart_specs() -> list[dict[str, object]]:
    def chart(
        chart_id: str,
        title: str,
        dataset: str,
        x: str,
        y_fields: tuple[str, ...],
        value_format: str,
        tooltip_fields: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "id": chart_id,
            "title": title,
            "type": "bar",
            "dataset": dataset,
            "sourceId": "diagnostic-json",
            "layout": "full",
            "valueFormat": value_format,
            "encodings": {
                "x": {"field": x, "type": "nominal"},
                "y": {"fields": list(y_fields), "type": "quantitative"},
                "tooltip": [
                    {"field": field, "type": "quantitative"}
                    for field in tooltip_fields
                ],
            },
        }

    return [
        chart(
            "paired-baselines",
            "동일 경기 기준선 비교",
            "baseline_pairs",
            "comparison",
            ("model_accuracy", "baseline_accuracy"),
            "percent",
            ("N", "delta", "mcnemar_p"),
        ),
        chart(
            "calibration",
            "예측 확률과 실제 적중률",
            "calibration_bins",
            "bin",
            ("mean_probability", "actual_accuracy"),
            "percent",
            ("N", "gap", "mean_brier"),
        ),
        chart(
            "monthly",
            "월별 Top 1 적중률",
            "monthly",
            "month",
            ("accuracy",),
            "percent",
            ("N", "W", "L"),
        ),
        chart(
            "components",
            "승패별 선택 측 컴포넌트 엣지",
            "component_edges",
            "component",
            ("wins_mean", "losses_mean"),
            "number",
            ("wins_N", "losses_N"),
        ),
    ]


def exact_table_specs() -> list[dict[str, object]]:
    return [
        {
            "id": "paired-baseline-table",
            "title": "짝지은 기준선 검정",
            "dataset": "paired_table",
            "sourceId": "diagnostic-json",
            "layout": "full",
            "density": "spacious",
            "defaultSort": {"field": "N", "direction": "desc"},
            "columns": [
                {"field": field, "label": label}
                for field, label in (
                    ("comparison", "비교"),
                    ("N", "N"),
                    ("model_only", "모델만 정답"),
                    ("baseline_only", "기준선만 정답"),
                    ("delta", "차이"),
                    ("mcnemar_p", "McNemar p"),
                )
            ],
        },
        {
            "id": "team-table",
            "title": "선택 팀별 결과",
            "dataset": "teams",
            "sourceId": "diagnostic-json",
            "layout": "full",
            "density": "comfortable",
            "defaultSort": {"field": "N", "direction": "desc"},
            "columns": [
                {"field": field, "label": label}
                for field, label in (
                    ("team", "팀"),
                    ("W", "승"),
                    ("L", "패"),
                    ("N", "N"),
                    ("accuracy", "적중률"),
                    ("wilson_low", "Wilson 하한"),
                    ("wilson_high", "Wilson 상한"),
                )
            ],
        },
    ]


def build_report_artifact(diagnostic: Mapping[str, object]) -> dict[str, object]:
    calibration = diagnostic["calibration"]
    assert isinstance(calibration, Mapping)
    bins = calibration["bins"]
    assert isinstance(bins, list)
    datasets = {
        "baseline_pairs": baseline_pair_rows(diagnostic),
        "calibration_bins": list(bins),
        "monthly": month_rows(diagnostic),
        "component_edges": component_rows(diagnostic),
        "paired_table": paired_table_rows(diagnostic),
        "teams": team_rows(diagnostic),
    }
    charts = exact_chart_specs()
    tables = exact_table_specs()
    blocks = [
        markdown_block("title", "# MLB v5.1 100픽 진단"),
        markdown_block(
            "executive-summary", executive_summary_body(diagnostic), sourced=True
        ),
        markdown_block("definitions", definitions_body(), sourced=True),
        markdown_block("baseline-finding", baseline_body(diagnostic), sourced=True),
        {
            "id": "paired-baselines-block",
            "type": "chart",
            "chartId": "paired-baselines",
        },
        {
            "id": "paired-baseline-table-block",
            "type": "table",
            "tableId": "paired-baseline-table",
        },
        markdown_block(
            "calibration-finding", calibration_body(diagnostic), sourced=True
        ),
        {"id": "calibration-block", "type": "chart", "chartId": "calibration"},
        markdown_block("stability-finding", stability_body(diagnostic), sourced=True),
        {"id": "monthly-block", "type": "chart", "chartId": "monthly"},
        {"id": "team-table-block", "type": "table", "tableId": "team-table"},
        markdown_block("component-finding", component_body(diagnostic), sourced=True),
        {"id": "components-block", "type": "chart", "chartId": "components"},
        markdown_block(
            "quality-leakage", quality_leakage_body(diagnostic), sourced=True
        ),
        markdown_block("next-steps", next_steps_body()),
        markdown_block("further-questions", further_questions_body()),
        markdown_block("caveats", caveats_body(diagnostic), sourced=True),
    ]
    manifest = {
        "version": 1,
        "surface": "report",
        "title": "MLB v5.1 100픽 진단",
        "generatedAt": None,
        "blocks": blocks,
        "charts": charts,
        "tables": tables,
        "sources": [REPORT_SOURCE],
    }
    snapshot = {
        "version": 1,
        "generatedAt": None,
        "status": "ready",
        "datasets": datasets,
    }
    return {
        "surface": "report",
        "manifest": manifest,
        "snapshot": snapshot,
        "sources": [REPORT_SOURCE],
    }


def build_chart_map() -> dict[str, object]:
    claims = {
        "paired-baselines": "season-record incremental delta is negligible",
        "calibration": "accuracy is not monotonic by probability bin",
        "monthly": "July is weaker and indicates instability",
        "components": "component differences are hypotheses, not retuning evidence",
    }
    result: dict[str, object] = {}
    for chart in exact_chart_specs():
        encodings = chart["encodings"]
        assert isinstance(encodings, Mapping)
        y = encodings["y"]
        assert isinstance(y, Mapping)
        y_fields = y["fields"]
        assert isinstance(y_fields, list)
        chart_id = chart["id"]
        assert isinstance(chart_id, str)
        result[chart_id] = {
            "question": chart["title"],
            "takeaway": claims[chart_id],
            "family": "grouped_bar" if len(y_fields) > 1 else "bar",
            "row_sufficiency": chart["dataset"],
            "encodings": encodings,
            "palette_policy": "shared categorical palette",
            "denominator_context": "N is exposed in tooltip/dataset",
            "source_id": chart["sourceId"],
            "adjacent_narrative_block": True,
            "footprint": "full-width responsive at laptop and mobile widths",
        }
    return result


def write_report_artifact(
    diagnostic_path: Path, artifact_path: Path, chart_map_path: Path
) -> None:
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    artifact = build_report_artifact(diagnostic)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    chart_map_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(canonical_json_bytes(artifact) + b"\n")
    chart_map_path.write_bytes(canonical_json_bytes(build_chart_map()) + b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--chart-map", type=Path, required=True)
    args = parser.parse_args(argv)
    write_report_artifact(args.diagnostic, args.artifact, args.chart_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
