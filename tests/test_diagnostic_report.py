from pathlib import Path
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from mlb_backtest.diagnostic_report import (
    iter_contract_tables,
    markdown_table,
    render_markdown,
    write_diagnostic_reports,
)
from mlb_backtest.diagnostics import build_diagnostic, load_v51_evidence


ROOT = Path(__file__).resolve().parents[1]


class DiagnosticReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = build_diagnostic(load_v51_evidence(ROOT))

    def test_report_is_deterministic_and_contains_frozen_conclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            first = write_diagnostic_reports(ROOT, Path(directory))
            second = write_diagnostic_reports(ROOT, Path(directory))
            self.assertEqual(first, second)
            report = (Path(directory) / "diagnostic_100.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("67승 33패", report)
            self.assertIn("4 대 3", report)
            self.assertIn("추가 예측 우위는 입증되지 않았다", report)
            self.assertIn("실제 T-45 재현성을 입증하지 않는다", report)
            self.assertIn("통계 as-of 집계 상태는 `LIMITATION`", report)
            self.assertIn("잠금→결과 결합 순서는 `NOT_TIMESTAMP_PROVEN`", report)
            self.assertIn("outcome-join 실행 타임스탬프가 없다", report)
            for forbidden in (
                "ROI가 양수",
                "CLV가 양수",
                "수익성이 입증",
                "시장 대비 우위가 입증",
            ):
                self.assertNotIn(forbidden, report)

    def test_regeneration_matches_both_committed_artifacts_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_diagnostic_reports(ROOT, output)
            for name in ("diagnostic_100.json", "diagnostic_100.md"):
                self.assertEqual(
                    (output / name).read_bytes(),
                    (ROOT / "reports/v5_1_bt" / name).read_bytes(),
                )

    def test_markdown_contains_complete_section_three_contract(self):
        report = render_markdown(self.payload)
        required = (
            "both correct 52",
            "model only 15",
            "baseline only 7",
            "both wrong 26",
            "+8.00pp",
            "z=1.97",
            "-1.10pp–+17.10pp",
            "both correct 62",
            "model only 4",
            "baseline only 3",
            "both wrong 30",
            "+1.0101pp",
            "-4.25pp–+6.27pp",
            "confidence–outcome Spearman ρ=0.0365, p=0.719",
            "≥63%: 59/82=71.9512%",
            "≥68%: 14/23=60.8696%",
            "monotonic calibration is not validated",
            "home 52/78=66.6667%",
            "away 15/22=68.1818%",
            "New York Yankees 14",
            "Philadelphia Phillies 11",
            "Houston Astros 9",
            "San Diego Padres 9",
            "Los Angeles Dodgers 7",
            "Seattle Mariners 7",
            "0.2358031605–0.5769861616",
            "mean 0.4602764334",
            "max_history_date <= D-1",
            "target gamePk Statcast excluded",
            "locked prediction rows contain no outcome fields",
            "bind exactly to later evaluated prediction fields",
            "proven PASS subchecks do not establish outcome-join chronology",
            "do not cure participant-time or source-revision limitations",
            "## Source hashes",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, report)
        for team, values in self.payload["teams"].items():
            low, high = values["wilson_95"]
            self.assertIn(
                f'| {team} | {values["N"]} | {values["W"]} | '
                f'{100 * values["accuracy"]:.4f}% | '
                f'{100 * low:.4f}%–{100 * high:.4f}% |',
                report,
            )
        for source in self.payload["sources"]:
            self.assertIn(source["path"], report)
            self.assertIn(source["sha256"], report)

    def test_each_registry_table_has_adjacent_contract_and_source_paths(self):
        report = render_markdown(self.payload)
        sources = {item["id"]: item["path"] for item in self.payload["sources"]}
        for name, table in self.payload["tables"].items():
            marker = f'### Table contract: `{name}`'
            self.assertEqual(report.count(marker), 1)
            block = report.split(marker, 1)[1].split("### Table contract:", 1)[0]
            self.assertIn(f'Population: `{table["population"]}`', block)
            self.assertIn(f'Formula: `{table["formula"]}`', block)
            self.assertIn("Definitions:", block)
            self.assertIn("Exclusions:", block)
            for source_id in table["source_ids"]:
                self.assertIn(sources[source_id], block)

    def test_paired_wins_are_exact_cells_and_incoherence_is_rejected(self):
        report = render_markdown(self.payload)
        self.assertIn("| home | 100 | 67/100 | 59/100 |", report)
        self.assertIn("| D-1 season record | 99 | 66/99 | 65/99 |", report)
        changed = copy.deepcopy(self.payload)
        changed["baselines"]["home"]["model_accuracy"] = 0.66
        with self.assertRaisesRegex(ValueError, "paired model accuracy"):
            render_markdown(changed)

    def test_publication_renders_both_before_replacing_and_cleans_temps(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            json_path = output / "diagnostic_100.json"
            md_path = output / "diagnostic_100.md"
            json_path.write_bytes(b"old-json")
            md_path.write_bytes(b"old-md")
            with patch(
                "mlb_backtest.diagnostic_report.render_markdown",
                side_effect=RuntimeError("render failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    write_diagnostic_reports(ROOT, output)
            self.assertEqual(json_path.read_bytes(), b"old-json")
            self.assertEqual(md_path.read_bytes(), b"old-md")
            self.assertEqual(list(output.glob(".diagnostic_100.*.tmp")), [])

    def test_publication_cleans_first_temp_when_second_temp_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            json_path = output / "diagnostic_100.json"
            md_path = output / "diagnostic_100.md"
            json_path.write_bytes(b"old-json")
            md_path.write_bytes(b"old-md")
            from mlb_backtest import diagnostic_report

            original = diagnostic_report._write_fsynced_temp
            calls = 0

            def fail_second(path, contents):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk failed")
                return original(path, contents)

            with patch(
                "mlb_backtest.diagnostic_report._write_fsynced_temp",
                side_effect=fail_second,
            ):
                with self.assertRaisesRegex(OSError, "disk failed"):
                    write_diagnostic_reports(ROOT, output)
            self.assertEqual(json_path.read_bytes(), b"old-json")
            self.assertEqual(md_path.read_bytes(), b"old-md")
            self.assertEqual(list(output.glob(".diagnostic_100.*.tmp")), [])

    def test_markdown_table_escapes_backslashes_newlines_and_pipes(self):
        rendered = markdown_table(["a\\b", "c|d"], [["x\ny", "z\\|q"]])
        self.assertIn("a\\\\b", rendered)
        self.assertIn("c\\|d", rendered)
        self.assertIn("x<br>y", rendered)
        self.assertIn("z\\\\\\|q", rendered)
        self.assertEqual(len(rendered.splitlines()), 3)

    def test_every_table_has_population_formula_exclusions_and_sources(self):
        payload = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(
                encoding="utf-8"
            )
        )
        for table in iter_contract_tables(payload):
            self.assertEqual(
                set(("population", "formula", "exclusions", "source_ids"))
                - set(table),
                set(),
            )


if __name__ == "__main__":
    unittest.main()
