from pathlib import Path
import json
import unittest

from mlb_backtest.diagnostics import (
    AUDITED_DIGEST_PATHS,
    PAIRED_INTERVAL_LABEL,
    audited_digest_map,
    exact_mcnemar,
    load_v51_evidence,
    paired_delta_interval,
    paired_table,
)


ROOT = Path(__file__).resolve().parents[1]


class DiagnosticPrimitiveTest(unittest.TestCase):
    def test_authoritative_inputs_match_audited_content_hashes(self):
        evidence = load_v51_evidence(ROOT)
        self.assertEqual(
            evidence.predictions_content_sha256,
            "bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb",
        )
        self.assertEqual(
            evidence.evaluated_content_sha256,
            "db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76",
        )
        self.assertEqual(len(evidence.evaluated_rows), 1340)
        self.assertEqual(
            sum(row["top1"] == "Yes" for row in evidence.evaluated_rows), 100
        )
        self.assertEqual(set(evidence.verified_file_sha256), set(AUDITED_DIGEST_PATHS))
        with self.assertRaises(TypeError):
            evidence.summary["primary"] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            evidence.model_card["counts"]["training_eligible_rows"] = 0  # type: ignore[index]

    def test_paired_table_mcnemar_and_delta_interval(self):
        table = paired_table(
            [True] * 52 + [True] * 15 + [False] * 7 + [False] * 26,
            [True] * 52 + [False] * 15 + [True] * 7 + [False] * 26,
        )
        self.assertEqual(
            table,
            {
                "both_correct": 52,
                "model_only": 15,
                "baseline_only": 7,
                "both_wrong": 26,
                "N": 100,
            },
        )
        self.assertAlmostEqual(exact_mcnemar(15, 7), 0.1338005066, places=9)
        self.assertEqual(
            tuple(round(100 * x, 2) for x in paired_delta_interval(15, 7, 100)),
            (-1.10, 17.10),
        )
        self.assertEqual(
            tuple(round(100 * x, 2) for x in paired_delta_interval(4, 3, 99)),
            (-4.25, 6.27),
        )
        self.assertEqual(
            PAIRED_INTERVAL_LABEL, "approximate paired 95% interval (z=1.97)"
        )

    def test_audited_digest_registry_rejects_schema_drift(self):
        audit_path = ROOT / "audit/v5_1_bt/lock_manifest.json"

        unknown = json.loads(audit_path.read_text(encoding="utf-8"))
        unknown["input_and_output_sha256"]["unexpected_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            audited_digest_map(unknown)

        missing = json.loads(audit_path.read_text(encoding="utf-8"))
        del missing["input_and_output_sha256"]["summary"]
        with self.assertRaises(ValueError):
            audited_digest_map(missing)

        duplicate = json.loads(audit_path.read_text(encoding="utf-8"))
        duplicate["sealed_lock"]["summary"] = duplicate[
            "input_and_output_sha256"
        ]["summary"]
        with self.assertRaisesRegex(ValueError, "more than one section"):
            audited_digest_map(duplicate)

    def test_statistical_counts_and_interval_parameters_are_strict(self):
        self.assertEqual(exact_mcnemar(0, 0), 1.0)
        self.assertEqual(paired_delta_interval(0, 0, 1), (0.0, 0.0))

        invalid_counts = (True, 1.0, "1", -1)
        for value in invalid_counts:
            with self.subTest(function="mcnemar_model", value=value):
                with self.assertRaises(ValueError):
                    exact_mcnemar(value, 0)  # type: ignore[arg-type]
            with self.subTest(function="mcnemar_baseline", value=value):
                with self.assertRaises(ValueError):
                    exact_mcnemar(0, value)  # type: ignore[arg-type]
            with self.subTest(function="interval_model", value=value):
                with self.assertRaises(ValueError):
                    paired_delta_interval(value, 0, 1)  # type: ignore[arg-type]
            with self.subTest(function="interval_baseline", value=value):
                with self.assertRaises(ValueError):
                    paired_delta_interval(0, value, 1)  # type: ignore[arg-type]

        for n in (True, 0, -1, 1.0, "1"):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    paired_delta_interval(0, 0, n)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            paired_delta_interval(1, 1, 1)

        for z in (True, 0, -1, float("inf"), float("nan"), "1.97"):
            with self.subTest(z=z):
                with self.assertRaises(ValueError):
                    paired_delta_interval(0, 0, 1, z)  # type: ignore[arg-type]

    def test_paired_table_rejects_non_boolean_elements(self):
        for model, baseline in (([1], [True]), ([True], [0]), ([None], [False])):
            with self.subTest(model=model, baseline=baseline):
                with self.assertRaises(ValueError):
                    paired_table(model, baseline)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
