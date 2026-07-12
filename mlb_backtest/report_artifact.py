from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlb_backtest.diagnostics import canonical_json_bytes


FROZEN_DIAGNOSTIC_SHA256 = (
    "9ac9279a18212ad9a82d5e8792001ba0fc068ff34c9a92a2cd0d4d35ab42dd91"
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FROZEN_DIAGNOSTIC_PATH = _REPOSITORY_ROOT / "reports/v5_1_bt/diagnostic_100.json"


MATERIALIZE_DATASETS_SQL = """\
WITH
input(document) AS (
    SELECT json(:diagnostic)
),
baseline_keys(baseline_key, label, ordinal) AS (
    VALUES
        ('home', '홈팀', 0),
        ('season_record', 'D-1 시즌 승률', 1)
),
baseline_rows AS (
    SELECT
        'baseline_pairs' AS dataset,
        ordinal,
        json_object(
            'comparison', label,
            '모델 적중률', json_extract(document, '$.baselines.' || baseline_key || '.model_accuracy'),
            '기준선 적중률', json_extract(document, '$.baselines.' || baseline_key || '.baseline_accuracy'),
            'N', json_extract(document, '$.baselines.' || baseline_key || '.paired.N'),
            'model_only', json_extract(document, '$.baselines.' || baseline_key || '.paired.model_only'),
            'baseline_only', json_extract(document, '$.baselines.' || baseline_key || '.paired.baseline_only'),
            'delta', json_extract(document, '$.baselines.' || baseline_key || '.delta'),
            'mcnemar_p', json_extract(document, '$.baselines.' || baseline_key || '.mcnemar_p')
        ) AS row_json
    FROM input CROSS JOIN baseline_keys
),
calibration_rows AS (
    SELECT
        'calibration_bins' AS dataset,
        CAST(bin.key AS INTEGER) AS ordinal,
        json_object(
            'bin', json_extract(bin.value, '$.bin'),
            '평균 예측 확률', json_extract(bin.value, '$.mean_probability'),
            '실제 적중률', json_extract(bin.value, '$.actual_accuracy'),
            'N', json_extract(bin.value, '$.N'),
            'gap', json_extract(bin.value, '$.gap'),
            'mean_brier', json_extract(bin.value, '$.mean_brier'),
            'brier_contribution', json_extract(bin.value, '$.brier_contribution')
        ) AS row_json
    FROM input, json_each(json_extract(document, '$.calibration.bins')) AS bin
),
monthly_rows AS (
    SELECT
        'monthly' AS dataset,
        row_number() OVER (ORDER BY month.key) - 1 AS ordinal,
        json_object(
            'month', month.key,
            '적중률', json_extract(month.value, '$.accuracy'),
            'W', json_extract(month.value, '$.record.W'),
            'L', json_extract(month.value, '$.record.L'),
            'N', json_extract(month.value, '$.record.N')
        ) AS row_json
    FROM input, json_each(json_extract(document, '$.monthly')) AS month
),
component_keys(component_key, component_label, ordinal) AS (
    VALUES
        ('starter', '선발', 0),
        ('offense', '타선', 1),
        ('bullpen', '불펜', 2),
        ('schedule', '일정', 3)
),
component_rows AS (
    SELECT
        'component_edges' AS dataset,
        ordinal,
        json_object(
            'component', component_label,
            '승리 평균 엣지', json_extract(document, '$.components.' || component_key || '.win_mean'),
            '패배 평균 엣지', json_extract(document, '$.components.' || component_key || '.loss_mean'),
            'wins_N', json_extract(document, '$.primary.record.W'),
            'losses_N', json_extract(document, '$.primary.record.L'),
            'unit', 'score points'
        ) AS row_json
    FROM input CROSS JOIN component_keys
),
paired_rows AS (
    SELECT
        'paired_table' AS dataset,
        ordinal,
        json_object(
            'comparison', label,
            'N', json_extract(document, '$.baselines.' || baseline_key || '.paired.N'),
            'baseline_only', json_extract(document, '$.baselines.' || baseline_key || '.paired.baseline_only'),
            'both_correct', json_extract(document, '$.baselines.' || baseline_key || '.paired.both_correct'),
            'both_wrong', json_extract(document, '$.baselines.' || baseline_key || '.paired.both_wrong'),
            'model_only', json_extract(document, '$.baselines.' || baseline_key || '.paired.model_only'),
            'delta', json_extract(document, '$.baselines.' || baseline_key || '.delta'),
            'interval_low', json_extract(document, '$.baselines.' || baseline_key || '.paired_delta_interval[0]'),
            'interval_high', json_extract(document, '$.baselines.' || baseline_key || '.paired_delta_interval[1]'),
            'mcnemar_p', json_extract(document, '$.baselines.' || baseline_key || '.mcnemar_p')
        ) AS row_json
    FROM input CROSS JOIN baseline_keys
),
team_ranked AS (
    SELECT
        team.key AS team,
        team.value AS values_json,
        row_number() OVER (
            ORDER BY CAST(json_extract(team.value, '$.N') AS INTEGER) DESC, team.key
        ) - 1 AS ordinal
    FROM input, json_each(json_extract(document, '$.teams')) AS team
),
team_rows AS (
    SELECT
        'teams' AS dataset,
        ordinal,
        json_object(
            'team', team,
            'W', json_extract(values_json, '$.W'),
            'L', json_extract(values_json, '$.L'),
            'N', json_extract(values_json, '$.N'),
            'accuracy', json_extract(values_json, '$.accuracy'),
            'wilson_low', json_extract(values_json, '$.wilson_95[0]'),
            'wilson_high', json_extract(values_json, '$.wilson_95[1]')
        ) AS row_json
    FROM team_ranked
),
all_rows AS (
    SELECT * FROM baseline_rows
    UNION ALL SELECT * FROM calibration_rows
    UNION ALL SELECT * FROM monthly_rows
    UNION ALL SELECT * FROM component_rows
    UNION ALL SELECT * FROM paired_rows
    UNION ALL SELECT * FROM team_rows
)
SELECT dataset, ordinal, row_json
FROM all_rows
ORDER BY
    CASE dataset
        WHEN 'baseline_pairs' THEN 0
        WHEN 'calibration_bins' THEN 1
        WHEN 'monthly' THEN 2
        WHEN 'component_edges' THEN 3
        WHEN 'paired_table' THEN 4
        WHEN 'teams' THEN 5
    END,
    ordinal
"""


REPORT_SOURCE: dict[str, object] = {
    "id": "diagnostic-json",
    "label": "Canonical v5.1 diagnostic",
    "path": "reports/v5_1_bt/diagnostic_100.json",
    "sha256": FROZEN_DIAGNOSTIC_SHA256,
    "query": {
        "engine": "sqlite-json1",
        "id": "v5.1-BT-diagnostic-100",
        "description": (
            "Executed SQLite JSON1 transformation that materializes all six "
            "bounded report datasets; :diagnostic is bound to the canonical "
            "UTF-8 JSON at reports/v5_1_bt/diagnostic_100.json."
        ),
        "sql": MATERIALIZE_DATASETS_SQL,
        "language": "sql",
        "executed_at": None,
        "tables_used": [
            "reports/v5_1_bt/diagnostic_100.json",
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


def require_frozen_diagnostic(diagnostic: Mapping[str, object]) -> None:
    digest = hashlib.sha256(canonical_json_bytes(diagnostic) + b"\n").hexdigest()
    if digest != FROZEN_DIAGNOSTIC_SHA256:
        raise ValueError(
            "report input does not match the frozen diagnostic_100.json identity"
        )


def materialize_datasets(
    diagnostic: Mapping[str, object],
) -> dict[str, list[dict[str, object]]]:
    """Execute the exact source query recorded in REPORT_SOURCE."""
    encoded = canonical_json_bytes(diagnostic).decode("utf-8")
    dataset_order = (
        "baseline_pairs",
        "calibration_bins",
        "monthly",
        "component_edges",
        "paired_table",
        "teams",
    )
    datasets: dict[str, list[dict[str, object]]] = {
        dataset: [] for dataset in dataset_order
    }
    with sqlite3.connect(":memory:") as connection:
        rows = connection.execute(
            MATERIALIZE_DATASETS_SQL, {"diagnostic": encoded}
        ).fetchall()
    for dataset, expected_ordinal, row_json in rows:
        if dataset not in datasets:
            raise ValueError(f"unexpected report dataset: {dataset!r}")
        if expected_ordinal != len(datasets[dataset]):
            raise ValueError(f"non-contiguous report dataset ordinal: {dataset!r}")
        row = json.loads(row_json)
        if not isinstance(row, dict):
            raise ValueError(f"report dataset row must be an object: {dataset!r}")
        datasets[dataset].append(row)
    return datasets


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
                "모델 적중률": value["model_accuracy"],
                "기준선 적중률": value["baseline_accuracy"],
                "N": paired["N"],
                "model_only": paired["model_only"],
                "baseline_only": paired["baseline_only"],
                "delta": value["delta"],
                "mcnemar_p": value["mcnemar_p"],
            }
        )
    return rows


def calibration_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    calibration = diagnostic["calibration"]
    assert isinstance(calibration, Mapping)
    bins = calibration["bins"]
    assert isinstance(bins, list)
    return [
        {
            "bin": value["bin"],
            "평균 예측 확률": value["mean_probability"],
            "실제 적중률": value["actual_accuracy"],
            "N": value["N"],
            "gap": value["gap"],
            "mean_brier": value["mean_brier"],
            "brier_contribution": value["brier_contribution"],
        }
        for value in bins
    ]


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
                "적중률": value["accuracy"],
                "W": record["W"],
                "L": record["L"],
                "N": record["N"],
            }
        )
    return rows


def component_rows(diagnostic: Mapping[str, object]) -> list[dict[str, object]]:
    components = diagnostic["components"]
    assert isinstance(components, Mapping)
    primary = diagnostic["primary"]
    assert isinstance(primary, Mapping)
    record = primary["record"]
    assert isinstance(record, Mapping)
    rows: list[dict[str, object]] = []
    for name, label in (
        ("starter", "선발"),
        ("offense", "타선"),
        ("bullpen", "불펜"),
        ("schedule", "일정"),
    ):
        value = components[name]
        assert isinstance(value, Mapping)
        rows.append(
            {
                "component": label,
                "승리 평균 엣지": value["win_mean"],
                "패배 평균 엣지": value["loss_mean"],
                "wins_N": record["W"],
                "losses_N": record["L"],
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
        "8월은 8월 12일까지 N=12인 부분 기간입니다. 이는 불안정성 "
        "진단이지 월/팀 효과의 증거가 아닙니다."
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
        x_label: str,
        y_fields: tuple[str, ...],
        y_label: str,
        value_format: str,
        unit: str,
        tooltip_fields: tuple[tuple[str, str, str], ...],
    ) -> dict[str, object]:
        return {
            "id": chart_id,
            "title": title,
            "type": "bar",
            "dataset": dataset,
            "sourceId": "diagnostic-json",
            "layout": "full",
            "valueFormat": value_format,
            "unit": unit,
            "encodings": {
                "x": {"field": x, "type": "nominal", "label": x_label},
                "y": {
                    "fields": list(y_fields),
                    "type": "quantitative",
                    "label": y_label,
                    "format": value_format,
                    "unit": unit,
                },
                "tooltip": [
                    {
                        "field": field,
                        "type": "quantitative",
                        "label": label,
                        "format": tooltip_format,
                    }
                    for field, label, tooltip_format in tooltip_fields
                ],
            },
        }

    return [
        chart(
            "paired-baselines",
            "동일 경기 기준선 비교",
            "baseline_pairs",
            "comparison",
            "비교 기준",
            ("모델 적중률", "기준선 적중률"),
            "적중률",
            "percent",
            "%",
            (
                ("N", "짝지은 경기 수", "number"),
                ("delta", "차이 (%p)", "percent"),
                ("mcnemar_p", "McNemar p", "number"),
            ),
        ),
        chart(
            "calibration",
            "예측 확률과 실제 적중률",
            "calibration_bins",
            "bin",
            "예측 확률 구간",
            ("평균 예측 확률", "실제 적중률"),
            "확률·적중률",
            "percent",
            "%",
            (
                ("N", "픽 수", "number"),
                ("gap", "보정 차이", "percent"),
                ("mean_brier", "평균 Brier", "number"),
            ),
        ),
        chart(
            "monthly",
            "월별 Top 1 적중률",
            "monthly",
            "month",
            "월",
            ("적중률",),
            "적중률",
            "percent",
            "%",
            (("N", "픽 수", "number"), ("W", "승", "number"), ("L", "패", "number")),
        ),
        chart(
            "components",
            "승패별 선택 측 컴포넌트 엣지",
            "component_edges",
            "component",
            "컴포넌트",
            ("승리 평균 엣지", "패배 평균 엣지"),
            "선택 측 평균 엣지",
            "number",
            "점",
            (("wins_N", "승리 표본", "number"), ("losses_N", "패배 표본", "number")),
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
                {"field": "comparison", "label": "비교", "type": "text"},
                {"field": "N", "label": "짝지은 경기 수", "format": "number"},
                {"field": "model_only", "label": "모델만 정답", "format": "number"},
                {
                    "field": "baseline_only",
                    "label": "기준선만 정답",
                    "format": "number",
                },
                {"field": "delta", "label": "차이 (%p)", "format": "percent"},
                {"field": "mcnemar_p", "label": "McNemar p", "format": "number"},
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
                {"field": "team", "label": "팀", "type": "text"},
                {"field": "W", "label": "승", "format": "number"},
                {"field": "L", "label": "패", "format": "number"},
                {"field": "N", "label": "픽 수", "format": "number"},
                {"field": "accuracy", "label": "적중률 (%)", "format": "percent"},
                {
                    "field": "wilson_low",
                    "label": "Wilson 하한 (%)",
                    "format": "percent",
                },
                {
                    "field": "wilson_high",
                    "label": "Wilson 상한 (%)",
                    "format": "percent",
                },
            ],
        },
    ]


def build_report_artifact(diagnostic: Mapping[str, object]) -> dict[str, object]:
    require_frozen_diagnostic(diagnostic)
    datasets = materialize_datasets(diagnostic)
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
    if diagnostic_path.resolve() != _FROZEN_DIAGNOSTIC_PATH.resolve():
        raise ValueError("--diagnostic must use the canonical diagnostic path")
    diagnostic_bytes = diagnostic_path.read_bytes()
    if hashlib.sha256(diagnostic_bytes).hexdigest() != FROZEN_DIAGNOSTIC_SHA256:
        raise ValueError(
            "canonical diagnostic file does not match frozen diagnostic identity"
        )
    diagnostic = json.loads(diagnostic_bytes.decode("utf-8"))
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
