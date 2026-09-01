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
        "evaluate_predictions(spec, candidate, baseline, outcome_receipt)",
        "load_evaluation_pair(spec, path, expected_sha256)",
    )
    DOC_API_REQUIREMENTS = {
        PLAN_PATHS[0]: RECEIPT_BOUND_APIS
        + (
            "OutcomeReceipt",
            "seal_outcome_receipt",
            "load_outcome_receipt",
            "load_outcome_rows",
            "load_evaluation_report",
        ),
        PLAN_PATHS[1]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[2]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[3]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[4]: RECEIPT_BOUND_APIS
        + (
            "OutcomeReceipt",
            "seal_outcome_receipt",
            "load_outcome_receipt",
            "load_outcome_rows",
            "open_h24_outcomes_once",
            "OutcomeCapability",
        ),
    }
    H24_CANONICAL_OUTCOME_SEAL_CALL = (
        "seal_outcome_receipt(spec, source_gate, common_feature_seal, baseline_replay, baseline_prediction_seal)"
    )
    H24_OPEN_ONCE_REQUIREMENTS = (
        "authorize_h24_outcome_open",
        "capability.consume_once()",
        "direct controller invocation without capability raises OutcomeCapabilityRequired",
        "second wrapper call raises SAME_REGISTRY_RETRY_FORBIDDEN",
        "ledger remains OUTCOMES_OPENED",
        "capability is not a Common API argument",
        "CLI/controller calls only open_h24_outcomes_once",
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
            "source_inventory, game_evidence_inventory",
            "source_inventory: SourceInventory",
            "game_evidence_inventory: GameEvidenceInventory",
        )
        for path, text in zip(PLAN_PATHS, texts):
            self.assertFalse(any(token in text for token in forbidden), path)
            self.assertNotRegex(text, r"\bsource_inventory\s*(?::|=|,|\))", path)
            self.assertNotRegex(text, r"\bgame_evidence_inventory\s*(?::|=|,|\))", path)

    def test_consumer_plans_preserve_the_staged_order_and_receipt_bindings(self):
        for path in PLAN_PATHS:
            text = " ".join(path.read_text(encoding="utf-8").split())
            positions = [text.index(stage) for stage in self.STAGES]
            self.assertEqual(positions, sorted(positions), path)
            for api in self.DOC_API_REQUIREMENTS[path]:
                self.assertIn(api, text, path)

        for path in PLAN_PATHS[1:]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("outcome_receipt_path", text, path)
            self.assertIn("outcome_receipt_sha256", text, path)
            self.assertRegex(text, r"(?:never[^\n]*outcomes_sha256|outcomes_sha256[^\n]*never)", path)

    def test_h24_uses_the_canonical_common_outcome_sealer_call(self):
        text = " ".join(PLAN_PATHS[-1].read_text(encoding="utf-8").split()).replace("`", "")
        self.assertIn(self.H24_CANONICAL_OUTCOME_SEAL_CALL, text)
        self.assertLess(text.index("capability.consume_once()"), text.index(self.H24_CANONICAL_OUTCOME_SEAL_CALL))
        self.assertIn(
            "capability.consume_once() outcome_receipt = " + self.H24_CANONICAL_OUTCOME_SEAL_CALL,
            text,
        )
        self.assertNotRegex(text, r"seal_outcome_receipt\([^)]*capability")
        for requirement in self.H24_OPEN_ONCE_REQUIREMENTS:
            self.assertIn(requirement, text)

    def test_h24_cli_has_no_outcome_file_or_fixture_input(self):
        text = PLAN_PATHS[-1].read_text(encoding="utf-8")
        task7 = text[text.index("### Task 7:") : text.index("### Task 8:")]
        for obsolete in (
            "outcome file",
            "outcome_file",
            "outcome fixture",
            "outcome_fixture",
            "tests/fixtures/",
            "--outcome-file",
            "--outcomes-path",
        ):
            self.assertNotIn(obsolete, task7)
        self.assertIn("Outcomes derive internally from the verified SourceInventory → GameEvidence", task7)
        self.assertIn("open_h24_outcomes_once", task7)


if __name__ == "__main__":
    unittest.main()
