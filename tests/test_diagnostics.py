from pathlib import Path
import copy
import json
import shutil
import tempfile
import unittest

from mlb_backtest.diagnostics import (
    AUDITED_DIGEST_PATHS,
    PAIRED_INTERVAL_LABEL,
    audit_leakage_evidence,
    audited_digest_map,
    build_diagnostic,
    canonical_json_bytes,
    chosen_side_edge,
    component_summary,
    exact_mcnemar,
    jsonable,
    load_v51_evidence,
    paired_delta_interval,
    paired_table,
    wilson,
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


class DiagnosticGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = build_diagnostic(load_v51_evidence(ROOT))

    def test_primary_and_exact_paired_baselines(self):
        self.assertEqual(self.result["primary"]["record"], {"W": 67, "L": 33, "N": 100})
        self.assertEqual(
            self.result["primary"]["wilson_95"],
            [0.5730516033971139, 0.7543701620610456],
        )
        self.assertAlmostEqual(self.result["primary"]["brier"], 0.22137165792176453)
        self.assertAlmostEqual(self.result["primary"]["log_loss"], 0.6351164919136184)
        self.assertEqual(
            self.result["baselines"]["home"]["paired"],
            {"both_correct": 52, "model_only": 15, "baseline_only": 7, "both_wrong": 26, "N": 100},
        )
        self.assertEqual(
            self.result["baselines"]["season_record"]["paired"],
            {"both_correct": 62, "model_only": 4, "baseline_only": 3, "both_wrong": 30, "N": 99},
        )
        self.assertEqual(self.result["baselines"]["market"]["N"], 0)

    def test_calibration_rank_time_team_component_and_quality(self):
        self.assertEqual(
            [(x["bin"], x["N"]) for x in self.result["calibration"]["bins"]],
            [("50-54", 0), ("54-58", 1), ("58-63", 17), ("63-68", 59), ("68-100", 23)],
        )
        self.assertEqual(
            [(x["rank"], x["W"], x["N"]) for x in self.result["rank"]],
            [(1, 67, 100), (2, 60, 100), (3, 59, 100)],
        )
        self.assertEqual(self.result["monthly"]["2025-07"]["record"], {"W": 14, "L": 13, "N": 27})
        self.assertEqual(self.result["team_concentration"]["top_six_share"], 0.57)
        self.assertAlmostEqual(self.result["components"]["bullpen"]["win_mean"], 11.752, places=3)
        self.assertEqual(
            self.result["data_quality"]["top1_states"],
            {"INCOMPLETE": 100, "High": 100, "Toss-up": 100},
        )
        self.assertAlmostEqual(
            self.result["data_quality"]["completeness"]["mean"], 0.4602764334, places=10
        )

    def test_calibration_separates_conditional_mean_from_overall_contribution(self):
        bins = self.result["calibration"]["bins"]
        self.assertTrue(all("mean_brier" in row for row in bins))
        self.assertTrue(all("brier_contribution" in row for row in bins))
        self.assertEqual(bins[0]["mean_brier"], None)
        self.assertEqual(bins[0]["brier_contribution"], 0.0)
        for row in bins:
            expected = (row["mean_brier"] or 0.0) * row["N"] / 100
            self.assertAlmostEqual(row["brier_contribution"], expected)
        self.assertAlmostEqual(
            sum(row["brier_contribution"] for row in bins),
            self.result["primary"]["brier"],
        )

    def test_all_candidate_quality_and_neutral_component_evidence(self):
        quality = self.result["data_quality"]
        self.assertEqual(
            quality["all_candidate_states"],
            {"N": 1340, "INCOMPLETE": 1340, "High": 1340, "Toss-up": 1340},
        )
        expected = {
            "N": 1340,
            "away_score_50": 1340,
            "home_score_50": 1340,
            "quality_zero": 1340,
        }
        self.assertEqual(quality["neutral_components"]["defense"], expected)
        self.assertEqual(quality["neutral_components"]["environment"], expected)

    def test_exact_secondary_statistics(self):
        self.assertEqual(
            self.result["sides"],
            {
                "home": {"W": 52, "L": 26, "N": 78, "accuracy": 52 / 78},
                "away": {"W": 15, "L": 7, "N": 22, "accuracy": 15 / 22},
            },
        )
        self.assertEqual(self.result["team_concentration"]["distinct_teams"], 23)
        self.assertEqual(
            self.result["team_concentration"]["top_six"],
            {
                "New York Yankees": 14,
                "Philadelphia Phillies": 11,
                "Houston Astros": 9,
                "San Diego Padres": 9,
                "Los Angeles Dodgers": 7,
                "Seattle Mariners": 7,
            },
        )
        self.assertEqual(self.result["team_concentration"]["top_six_share"], 0.57)
        self.assertEqual(
            [round(self.result["discrimination"]["probability_spearman"][k], 4 if k == "rho" else 3) for k in ("rho", "p")],
            [0.0077, 0.939],
        )
        self.assertEqual(
            [round(self.result["discrimination"]["legacy_confidence_spearman"][k], 4 if k == "rho" else 3) for k in ("rho", "p")],
            [0.0365, 0.719],
        )
        self.assertEqual(
            self.result["thresholds"]["ge_63"],
            {"W": 59, "N": 82, "accuracy": 59 / 82, "post_hoc": True},
        )
        self.assertEqual(
            self.result["thresholds"]["ge_68"],
            {"W": 14, "N": 23, "accuracy": 14 / 23, "post_hoc": True},
        )
        self.assertEqual([round(x["p"], 3) for x in self.result["rank_comparisons"]], [0.349, 0.291])
        self.assertEqual(round(self.result["temporal"]["july_vs_other_fisher_p"], 4), 0.0589)
        self.assertEqual(
            self.result["components"]["bullpen"]["absolute_edge_halves"],
            {
                "lower": {"W": 37, "N": 50, "accuracy": 0.74},
                "upper": {"W": 30, "N": 50, "accuracy": 0.60},
            },
        )
        self.assertEqual(
            [round(self.result["components"]["bullpen"]["spearman"][k], 3) for k in ("rho", "p")],
            [-0.151, 0.135],
        )

    def test_chosen_side_component_edges_are_signed_not_absolute(self):
        synthetic = [
            {"predicted_side": "home", "correct": True, "starter_edge": 5.0},
            {"predicted_side": "away", "correct": False, "starter_edge": 7.0},
        ]
        self.assertEqual(chosen_side_edge(synthetic[0], "starter"), 5.0)
        self.assertEqual(chosen_side_edge(synthetic[1], "starter"), -7.0)
        summary = component_summary(synthetic, "starter")
        self.assertEqual(summary["win_mean"], 5.0)
        self.assertEqual(summary["loss_mean"], -7.0)

    def test_leakage_categories_are_distinct_and_exact(self):
        self.assertEqual(
            set(self.result["leakage"]),
            {"statistical_as_of_safety", "participant_time_limitation", "source_revision_limitation"},
        )
        safety = self.result["leakage"]["statistical_as_of_safety"]
        self.assertEqual(safety["status"], "LIMITATION")
        self.assertEqual(safety["d1_feature_isolation_status"], "PASS")
        self.assertEqual(safety["locked_prediction_outcome_fields_status"], "PASS")
        self.assertEqual(safety["lock_to_outcome_join_sequence_status"], "NOT_TIMESTAMP_PROVEN")
        self.assertEqual(self.result["leakage"]["participant_time_limitation"]["status"], "LIMITATION")
        self.assertEqual(self.result["leakage"]["source_revision_limitation"]["status"], "LIMITATION")

    def test_leakage_evidence_tampering_fails(self):
        evidence = load_v51_evidence(ROOT)
        raw = jsonable(evidence.candidate_rows[0])
        card = jsonable(evidence.model_card)
        card["counts"]["candidate_rows"] = 1
        manual = evidence.manual_audit_text

        def audit(candidate_rows, *, locked=None, evaluated=None, audit=None):
            return audit_leakage_evidence(
                candidate_rows,
                card,
                manual,
                locked_prediction_rows=(locked or evidence.locked_prediction_rows),
                evaluated_rows=(evaluated or evidence.evaluated_rows),
                lock_audit=(audit or evidence.lock_audit),
            )

        for mutate in (
            lambda x: x["provenance"].__setitem__("max_history_date", x["official_date"]),
            lambda x: x["provenance"].__setitem__("statcast_target_excluded", False),
            lambda x: x.__setitem__("lineup_provenance", "TIMESTAMP_VERIFIED_T45"),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, "leakage evidence"):
                audit((changed,))
        with self.assertRaisesRegex(ValueError, "manual audit evidence"):
            audit_leakage_evidence(
                (raw,),
                card,
                manual.replace("2025-06-15", "missing"),
                locked_prediction_rows=evidence.locked_prediction_rows,
                evaluated_rows=evidence.evaluated_rows,
                lock_audit=evidence.lock_audit,
            )

        locked = jsonable(evidence.locked_prediction_rows)
        locked[0]["outcome"] = {"actual_winner": "forbidden"}
        with self.assertRaisesRegex(ValueError, "locked prediction outcome isolation"):
            audit((raw,), locked=locked)

        mismatched = jsonable(evidence.evaluated_rows)
        mismatched[0]["predicted_winner"] = "tampered"
        with self.assertRaisesRegex(ValueError, "locked/evaluated prediction binding"):
            audit((raw,), evaluated=mismatched)

        malformed_audit = jsonable(evidence.lock_audit)
        del malformed_audit["sealed_lock"]["created_at_utc"]
        with self.assertRaisesRegex(ValueError, "lock sequencing evidence"):
            audit((raw,), audit=malformed_audit)

    def test_loaded_evidence_is_snapshot_bound_after_source_mutation(self):
        paths = set(AUDITED_DIGEST_PATHS.values()) | {
            "audit/v5_1_bt/lock_manifest.json",
            "audit/v5_1_bt/manual_audit.md",
            "config/v5_1_bt.json",
        }
        with tempfile.TemporaryDirectory() as td:
            copied_root = Path(td)
            for relative in paths:
                target = copied_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            evidence = load_v51_evidence(copied_root)
            before = canonical_json_bytes(build_diagnostic(evidence))
            for relative in (
                "data/v5_1_bt/features/candidate_raw_features.jsonl",
                "data/v5_1_bt/locked/predictions.jsonl",
                "audit/v5_1_bt/manual_audit.md",
                "config/v5_1_bt.json",
            ):
                (copied_root / relative).write_text("tampered\n", encoding="utf-8")
            after = canonical_json_bytes(build_diagnostic(evidence))
            self.assertEqual(after, before)

    def test_all_chosen_side_means_team_intervals_and_training_count(self):
        self.assertEqual(
            {
                name: (round(v["win_mean"], 3), round(v["loss_mean"], 3))
                for name, v in self.result["components"].items()
                if name in {"starter", "offense", "bullpen", "schedule"}
            },
            {
                "starter": (14.879, 13.751),
                "offense": (8.287, 7.049),
                "bullpen": (11.752, 14.238),
                "schedule": (1.313, 2.424),
            },
        )
        for team, row in self.result["teams"].items():
            self.assertEqual(row["wilson_95"], list(wilson(row["W"], row["N"])), team)
        self.assertEqual(self.result["data_quality"]["training_eligible_rows"], 7265)


if __name__ == "__main__":
    unittest.main()
