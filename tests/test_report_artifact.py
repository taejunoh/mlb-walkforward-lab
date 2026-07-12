from pathlib import Path
import copy
import json
import tempfile
import unittest

from mlb_backtest.report_artifact import (
    FROZEN_DIAGNOSTIC_SHA256,
    MATERIALIZE_DATASETS_SQL,
    baseline_pair_rows,
    build_chart_map,
    build_report_artifact,
    calibration_rows,
    component_rows,
    materialize_datasets,
    month_rows,
    paired_table_rows,
    team_rows,
    write_report_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC = ROOT / "reports/v5_1_bt/diagnostic_100.json"


class ReportArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostic = json.loads(DIAGNOSTIC.read_text(encoding="utf-8"))
        cls.artifact = build_report_artifact(cls.diagnostic)

    def test_artifact_is_one_executive_report_with_bounded_canonical_sources(self):
        artifact = self.artifact
        self.assertEqual(set(artifact), {"surface", "manifest", "snapshot", "sources"})
        self.assertEqual(artifact["surface"], "report")
        self.assertEqual(
            set(artifact["manifest"]),
            {
                "version",
                "surface",
                "title",
                "generatedAt",
                "blocks",
                "charts",
                "tables",
                "sources",
            },
        )
        self.assertEqual(artifact["manifest"]["version"], 1)
        self.assertIsNone(artifact["manifest"]["generatedAt"])
        self.assertIsNone(artifact["snapshot"]["generatedAt"])
        self.assertNotIn("audience", artifact["manifest"])
        blocks = artifact["manifest"]["blocks"]
        self.assertEqual(
            blocks[0],
            {"id": "title", "type": "markdown", "body": "# MLB v5.1 100픽 진단"},
        )
        self.assertTrue(blocks[1]["body"].startswith("## Executive Summary"))
        self.assertEqual(
            [block.get("chartId") for block in blocks if block["type"] == "chart"],
            ["paired-baselines", "calibration", "monthly", "components"],
        )
        self.assertEqual(
            [block.get("tableId") for block in blocks if block["type"] == "table"],
            ["paired-baseline-table", "team-table"],
        )
        self.assertEqual(
            [chart["id"] for chart in artifact["manifest"]["charts"]],
            ["paired-baselines", "calibration", "monthly", "components"],
        )
        self.assertEqual(
            set(artifact["snapshot"]),
            {"version", "generatedAt", "status", "datasets"},
        )
        self.assertEqual(artifact["snapshot"]["version"], 1)
        self.assertEqual(artifact["snapshot"]["status"], "ready")
        self.assertNotIn("accessIssues", artifact["snapshot"])
        self.assertLessEqual(
            sum(len(rows) for rows in artifact["snapshot"]["datasets"].values()),
            100,
        )
        source_ids = {source["id"] for source in artifact["sources"]} | {
            source["id"] for source in artifact["manifest"]["sources"]
        }
        self.assertTrue(
            all(
                item["sourceId"] in source_ids
                for item in artifact["manifest"]["charts"]
                + artifact["manifest"]["tables"]
            )
        )
        serialized = json.dumps(artifact)
        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn("../", serialized)

    def test_installed_chart_and_table_contract_is_canonical(self):
        artifact = self.artifact
        charts = artifact["manifest"]["charts"]
        self.assertFalse(
            any("series" in chart or "xField" in chart for chart in charts)
        )
        self.assertTrue(all("field" in chart["encodings"]["x"] for chart in charts))
        self.assertTrue(
            all(
                bool(
                    chart["encodings"]["y"].get("field")
                    or chart["encodings"]["y"].get("fields")
                )
                for chart in charts
            )
        )
        self.assertEqual(
            next(chart for chart in charts if chart["id"] == "components")[
                "valueFormat"
            ],
            "number",
        )
        self.assertTrue(
            all(
                table["density"] in {"compact", "comfortable", "spacious"}
                for table in artifact["manifest"]["tables"]
            )
        )
        blocks = artifact["manifest"]["blocks"]
        self.assertEqual(
            [blocks[1]["id"], blocks[2]["id"]],
            ["executive-summary", "definitions"],
        )
        self.assertIn("모집단", blocks[2]["body"])
        self.assertIn("분모", blocks[2]["body"])
        quality = next(block for block in blocks if block["id"] == "quality-leakage")
        self.assertIn("`NOT_TIMESTAMP_PROVEN`", quality["body"])
        self.assertIn("`LIMITATION`", quality["body"])

    def test_source_query_is_the_executed_six_dataset_transformation(self):
        source = self.artifact["sources"][0]
        query = source["query"]
        self.assertEqual(query["engine"], "sqlite-json1")
        self.assertEqual(query["language"], "sql")
        self.assertEqual(query["sql"], MATERIALIZE_DATASETS_SQL)
        self.assertNotIn("read_json_auto", query["sql"])
        self.assertNotIn("python-stdlib", query["engine"])
        datasets = materialize_datasets(self.diagnostic)
        self.assertEqual(datasets, self.artifact["snapshot"]["datasets"])
        self.assertEqual(
            set(datasets),
            {
                "baseline_pairs",
                "calibration_bins",
                "monthly",
                "component_edges",
                "paired_table",
                "teams",
            },
        )
        self.assertEqual(
            datasets["baseline_pairs"], baseline_pair_rows(self.diagnostic)
        )
        self.assertEqual(
            datasets["calibration_bins"], calibration_rows(self.diagnostic)
        )
        self.assertEqual(datasets["monthly"], month_rows(self.diagnostic))
        self.assertEqual(datasets["component_edges"], component_rows(self.diagnostic))
        self.assertEqual(datasets["paired_table"], paired_table_rows(self.diagnostic))
        self.assertEqual(datasets["teams"], team_rows(self.diagnostic))

    def test_report_rejects_any_diagnostic_content_or_source_identity_drift(self):
        for path, value in (
            (("primary", "record", "W"), 66),
            (("monthly", "2025-08", "record", "N"), 13),
            (("components", "bullpen", "win_mean"), 999.0),
        ):
            with self.subTest(path=path):
                changed = copy.deepcopy(self.diagnostic)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaisesRegex(ValueError, "frozen diagnostic"):
                    build_report_artifact(changed)

        source = self.artifact["sources"][0]
        self.assertEqual(source["path"], "reports/v5_1_bt/diagnostic_100.json")
        self.assertEqual(source["sha256"], FROZEN_DIAGNOSTIC_SHA256)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "diagnostic_100.json"
            copied.write_bytes(DIAGNOSTIC.read_bytes())
            with self.assertRaisesRegex(ValueError, "canonical diagnostic path"):
                write_report_artifact(
                    copied,
                    Path(directory) / "artifact.json",
                    Path(directory) / "chart-map.json",
                )

    def test_chart_series_categories_and_numeric_formats_are_reader_facing(self):
        charts = {chart["id"]: chart for chart in self.artifact["manifest"]["charts"]}
        self.assertEqual(
            charts["paired-baselines"]["encodings"]["y"]["fields"],
            ["모델 적중률", "기준선 적중률"],
        )
        self.assertEqual(
            charts["calibration"]["encodings"]["y"]["fields"],
            ["평균 예측 확률", "실제 적중률"],
        )
        self.assertEqual(
            charts["components"]["encodings"]["y"]["fields"],
            ["승리 평균 엣지", "패배 평균 엣지"],
        )
        self.assertEqual(
            [
                row["component"]
                for row in self.artifact["snapshot"]["datasets"]["component_edges"]
            ],
            ["선발", "타선", "불펜", "일정"],
        )
        for row in self.artifact["snapshot"]["datasets"]["component_edges"]:
            with self.subTest(component=row["component"]):
                self.assertIsInstance(row["wins_mean"], (int, float))
                self.assertIsInstance(row["losses_mean"], (int, float))
                self.assertEqual(row["wins_mean"], row["승리 평균 엣지"])
                self.assertEqual(row["losses_mean"], row["패배 평균 엣지"])
        for chart_id in ("paired-baselines", "calibration", "monthly"):
            with self.subTest(chart=chart_id):
                chart = charts[chart_id]
                self.assertEqual(chart["valueFormat"], "percent")
                self.assertEqual(chart["unit"], "%")
                self.assertEqual(chart["encodings"]["y"]["format"], "percent")
                self.assertEqual(chart["encodings"]["y"]["unit"], "%")
        component_chart = charts["components"]
        self.assertEqual(component_chart["valueFormat"], "number")
        self.assertEqual(component_chart["unit"], "점")
        self.assertEqual(component_chart["encodings"]["y"]["format"], "number")
        self.assertEqual(component_chart["encodings"]["y"]["unit"], "점")

        tables = {table["id"]: table for table in self.artifact["manifest"]["tables"]}
        paired_columns = {
            column["field"]: column
            for column in tables["paired-baseline-table"]["columns"]
        }
        self.assertEqual(paired_columns["delta"]["format"], "percent")
        self.assertIn("%p", paired_columns["delta"]["label"])
        self.assertEqual(paired_columns["mcnemar_p"]["format"], "number")
        team_columns = {
            column["field"]: column for column in tables["team-table"]["columns"]
        }
        for field in ("accuracy", "wilson_low", "wilson_high"):
            self.assertEqual(team_columns[field]["format"], "percent")
            self.assertIn("%", team_columns[field]["label"])

    def test_august_partial_period_is_explicit_in_adjacent_narrative(self):
        body = next(
            block["body"]
            for block in self.artifact["manifest"]["blocks"]
            if block["id"] == "stability-finding"
        )
        self.assertIn("8월 12일까지", body)
        self.assertIn("N=12", body)

    def test_charts_have_rich_data_and_immediately_adjacent_narratives(self):
        artifact = self.artifact
        datasets = artifact["snapshot"]["datasets"]
        blocks = artifact["manifest"]["blocks"]
        chart_positions = {
            block["chartId"]: index
            for index, block in enumerate(blocks)
            if block["type"] == "chart"
        }
        expected_predecessors = {
            "paired-baselines": "baseline-finding",
            "calibration": "calibration-finding",
            "monthly": "stability-finding",
            "components": "component-finding",
        }
        for chart in artifact["manifest"]["charts"]:
            with self.subTest(chart=chart["id"]):
                rows = datasets[chart["dataset"]]
                plotted = {chart["encodings"]["x"]["field"]} | set(
                    chart["encodings"]["y"].get("fields", [])
                )
                self.assertTrue(any(set(row) - plotted for row in rows))
                self.assertEqual(
                    blocks[chart_positions[chart["id"]] - 1]["id"],
                    expected_predecessors[chart["id"]],
                )

    def test_chart_map_captures_visual_qa_contract(self):
        chart_map = build_chart_map()
        self.assertEqual(
            set(chart_map),
            {"paired-baselines", "calibration", "monthly", "components"},
        )
        for chart_id, contract in chart_map.items():
            with self.subTest(chart=chart_id):
                self.assertTrue(contract["question"])
                self.assertTrue(contract["takeaway"])
                self.assertTrue(contract["family"])
                self.assertTrue(contract["row_sufficiency"])
                self.assertTrue(contract["encodings"])
                self.assertTrue(contract["palette_policy"])
                self.assertTrue(contract["denominator_context"])
                self.assertEqual(contract["source_id"], "diagnostic-json")
                self.assertTrue(contract["adjacent_narrative_block"])
                self.assertTrue(contract["footprint"])

    def test_writer_is_deterministic_and_matches_committed_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifact = output / "artifact.json"
            chart_map = output / "chart-map.json"
            write_report_artifact(DIAGNOSTIC, artifact, chart_map)
            first = (artifact.read_bytes(), chart_map.read_bytes())
            write_report_artifact(DIAGNOSTIC, artifact, chart_map)
            self.assertEqual(first, (artifact.read_bytes(), chart_map.read_bytes()))
            self.assertEqual(
                artifact.read_bytes(),
                (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_bytes(),
            )
            self.assertEqual(
                chart_map.read_bytes(),
                (ROOT / "reports/v5_1_bt/diagnostic_100_chart_map.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
