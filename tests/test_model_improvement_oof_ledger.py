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
        )
        for text in texts:
            self.assertFalse(any(token in text for token in forbidden), text)

    def test_consumer_plans_preserve_the_staged_order_and_receipt_bindings(self):
        combined = "\n".join(path.read_text(encoding="utf-8") for path in PLAN_PATHS)
        stages = (
            "Task8A Common OOF FeatureSeal admission + baseline label-free seals/loaders",
            "Task6A baseline replay receipt after its matching baseline seal",
            "Task8B nonbaseline label-free seals/loaders after same-fold replay receipt",
            "Task6B SourceInventory → GameEvidence outcomes, reports, and final pair",
        )
        positions = [combined.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions))
        for api in (
            "seal_oof_predictions(spec, source_gate, feature_seal, baseline_replay",
            "load_prediction_seal(spec, source_gate, feature_seal, baseline_replay",
            "load_prediction_rows(spec, source_gate, feature_seal, baseline_replay",
            "OutcomeReceipt",
            "load_outcome_receipt",
            "seal_outcome_receipt",
            "evaluate_predictions(spec, candidate, baseline, outcome_receipt)",
            "outcome_receipt_path",
            "outcome_receipt_sha256",
        ):
            self.assertIn(api, combined)
        self.assertRegex(combined, r"(?:never[^\n]*outcomes_sha256|outcomes_sha256[^\n]*never)")


if __name__ == "__main__":
    unittest.main()
