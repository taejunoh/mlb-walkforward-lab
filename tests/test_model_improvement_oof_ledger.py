"""Documentation contracts for the staged OOF prediction/evaluation boundary."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATHS = (
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md",
)


class OofLedgerDocumentationContractTest(unittest.TestCase):
    """Keep consumer plans on the receipt-bound, label-free Task8 contract."""

    STAGES = (
        "Task8A Common OOF FeatureSeal admission + baseline label-free seals/loaders",
        "Task6A baseline replay receipt after its matching baseline seal",
        "Task8B nonbaseline label-free seals/loaders after same-fold replay receipt",
        "Task6B SourceInventory → GameEvidence outcomes, reports, and final pair",
    )
    RECEIPT_BOUND_APIS = (
        "seal_oof_predictions(spec, source_gate, feature_seal, baseline_replay",
        "load_prediction_seal(spec, source_gate, feature_seal, baseline_replay",
        "load_prediction_rows(spec, source_gate, feature_seal, baseline_replay",
        "OutcomeReceipt",
        "load_outcome_receipt",
        "seal_outcome_receipt",
        "evaluate_predictions(spec, candidate, baseline, outcome_receipt)",
        "load_evaluation_pair(spec, path, expected_sha256)",
    )

    def test_consumer_plans_name_only_the_receipt_bound_task8_task6_interfaces(self):
        texts = [path.read_text(encoding="utf-8") for path in PLAN_PATHS]
        forbidden = (
            "join_oof_outcomes(",
            "JoinedLedger",
            "load_joined_ledger(",
            "load_joined_prediction_rows(",
            "outcomes: Mapping",
            "labels:",
            "seal_oof_predictions(spec, ledger_id, rows)",
            "load_prediction_rows(spec, seal)",
            "evaluate_predictions(... outcomes)",
            "evaluate_predictions(spec, source_gate, candidate, baseline, outcomes)",
            "seal_h24_outcome_receipt",
            "H24OutcomeRow",
            "EvaluatedPredictionRow",
            "outcome_rows: Sequence",
            "Sequence[H24OutcomeRow]",
            "Sequence[EvaluatedPredictionRow]",
        )
        for path, text in zip(PLAN_PATHS, texts):
            self.assertFalse(any(token in text for token in forbidden), path)

    def test_consumer_plans_preserve_the_staged_order_and_receipt_bindings(self):
        for path in PLAN_PATHS:
            text = " ".join(path.read_text(encoding="utf-8").split())
            positions = [text.index(stage) for stage in self.STAGES]
            self.assertEqual(positions, sorted(positions), path)
            for api in self.RECEIPT_BOUND_APIS:
                self.assertIn(api, text, path)

        for path in PLAN_PATHS[1:]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("outcome_receipt_path", text, path)
            self.assertIn("outcome_receipt_sha256", text, path)
            self.assertRegex(text, r"(?:never[^\n]*outcomes_sha256|outcomes_sha256[^\n]*never)", path)


if __name__ == "__main__":
    unittest.main()
