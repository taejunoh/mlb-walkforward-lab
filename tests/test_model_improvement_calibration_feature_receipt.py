"""Closed parsing tests for pre-RunSpec calibration feature receipts."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from mlb_backtest.model_improvement.calibration_feature_receipts import (
    CalibrationFeatureReceiptV1,
    CalibrationFeatureRowV1,
)
from mlb_backtest.model_improvement.canonical import canonical_json_bytes


class CalibrationFeatureReceiptTest(unittest.TestCase):
    def _row(self, pk: int) -> dict[str, object]:
        digest = "a" * 64
        row: dict[str, object] = {
            "schema": "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_ROW_V1",
            "model_id": "v5.1",
            "lane_id": "baseline",
            "fold_id": "O19",
            "calibration_key": "C19",
            "game_pk": pk,
            "official_date": "2018-04-01",
            "scheduled_start_utc": "2018-04-01T17:00:00Z",
            "max_history_date": "2018-03-31",
            "target_year": 2019,
            "history_start_date": "2018-01-01",
            "history_end_date": "2018-03-31",
            "home_team_id": 1,
            "away_team_id": 2,
            "component_scores": {
                side: {
                    c: 1.0
                    for c in (
                        "starter",
                        "offense",
                        "bullpen",
                        "environment",
                        "defense",
                        "schedule",
                    )
                }
                for side in ("home", "away")
            },
            "component_qualities": {
                side: {
                    c: 1.0
                    for c in (
                        "starter",
                        "offense",
                        "bullpen",
                        "environment",
                        "defense",
                        "schedule",
                    )
                }
                for side in ("home", "away")
            },
            "source_feature_file_sha256": digest,
            "source_feature_content_sha256": digest,
            "source_authority_sha256": digest,
        }
        row["feature_row_sha256"] = sha256(canonical_json_bytes(row)).hexdigest()
        return row

    def test_row_digest_is_its_complete_label_free_preimage(self) -> None:
        row = self._row(1)
        self.assertEqual(
            CalibrationFeatureRowV1.from_mapping(row).payload["game_pk"], 1
        )
        with self.assertRaises(ValueError):
            CalibrationFeatureRowV1.from_mapping(row | {"away_team_id": 3})

    def test_receipt_rejects_noncanonical_or_wrong_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "calibration_feature_receipt.json"
            sibling = root / "calibration_feature_rows.jsonl"
            row = self._row(1)
            rows = canonical_json_bytes(row)
            fields = {
                "schema": "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_RECEIPT_V1",
                "model_id": "v5.1",
                "lane_id": "baseline",
                "fold_id": "O19",
                "calibration_key": "C19",
                "target_year": 2019,
                "training_start": "2018-01-01",
                "training_end": "2018-12-31",
                "source_authority_sha256": "a" * 64,
                "source_inventory_sha256": "a" * 64,
                "game_evidence_inventory_relative_path": "evidence.json",
                "game_evidence_inventory_sha256": "a" * 64,
                "feature_contract_sha256": "a" * 64,
                "feature_code_revision": "abc",
                "feature_rows_path": str(sibling),
                "feature_rows_sha256": sha256(rows).hexdigest(),
                "feature_population_sha256": sha256(
                    canonical_json_bytes([1])
                ).hexdigest(),
            }
            primary.write_bytes(canonical_json_bytes(fields))
            sibling.write_bytes(rows)
            receipt = CalibrationFeatureReceiptV1.from_bytes(
                primary.read_bytes(), primary, rows
            )
            self.assertEqual(receipt.path, str(primary))
            bad = fields | {"feature_rows_path": str(root / "other.jsonl")}
            with self.assertRaises(ValueError):
                CalibrationFeatureReceiptV1.from_bytes(
                    canonical_json_bytes(bad), primary, rows
                )
