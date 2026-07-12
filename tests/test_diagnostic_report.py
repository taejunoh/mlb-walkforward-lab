from pathlib import Path
import json
import tempfile
import unittest

from mlb_backtest.diagnostic_report import (
    iter_contract_tables,
    write_diagnostic_reports,
)


ROOT = Path(__file__).resolve().parents[1]


class DiagnosticReportTest(unittest.TestCase):
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
