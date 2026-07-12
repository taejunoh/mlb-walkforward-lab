from pathlib import Path
import copy
import json
import tempfile
import unittest

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.protocols import (
    CHANGE_CHANGED,
    CHANGE_FORBIDDEN,
    CHANGE_UNCHANGED,
    V52_REQUIRED_KEYS,
    classify_uncertainty,
    classify_v52,
    data_completeness_score,
    direction_from_p_home,
    eligibility_disposition,
    load_change_contract,
    load_v52_config,
    optional_neutral,
    parse_change_contract,
    parse_v52_config,
    ranking_key,
    read_legacy_confidence,
    write_v52_config_contracts,
)


ROOT = Path(__file__).resolve().parents[1]
V52_CONFIG = ROOT / "config/v5_2_qd.json"
V52_SCHEMA = ROOT / "schemas/v5_2_qd.schema.json"
CHANGE_CONTRACT = ROOT / "protocols/v5_2_qd/change_contract.json"


class V52ConfigTest(unittest.TestCase):
    def test_v52_preserves_prediction_structure_but_has_no_fitted_values(self):
        config = load_v52_config(V52_CONFIG)
        v51 = json.loads((ROOT / "config/v5_1_bt.json").read_text())
        self.assertEqual(config.component_weights, v51["component_weights"])
        self.assertEqual(config.shrinkage_constants, v51["shrinkage_constants"])
        self.assertEqual(config.probability_clip, (0.10, 0.90))
        self.assertNotIn("normalization", config.raw)
        self.assertNotIn("slot_weights", config.raw)
        serialized = canonical_json_bytes(config.raw).decode("utf-8")
        self.assertNotIn('"alpha"', serialized)
        self.assertNotIn('"beta"', serialized)
        self.assertNotIn("fitted_model_cards", serialized)

    def test_completeness_uncertainty_and_tier_boundaries(self):
        qualities = {
            "starter": 1,
            "offense": 1,
            "bullpen": 1,
            "environment": 0,
            "defense": 0,
            "schedule": 1,
        }
        self.assertEqual(data_completeness_score(qualities), 8.5)
        self.assertEqual(classify_v52(0.63, 8.5, "Medium", critical=False), "A")
        self.assertEqual(classify_v52(0.68, 8.99, "Medium", critical=False), "A")
        self.assertEqual(classify_v52(0.70, 9.0, "Low", critical=True), "UNRATED")
        self.assertEqual(classify_v52(0.70, 7.99, "High", critical=False), "Toss-up")

    def test_eligibility_dispositions_are_frozen_before_outcomes(self):
        cases = {
            "missing_game_id": ("EXCLUDE", "MISSING_GAME_ID"),
            "invalid_competition": ("EXCLUDE", "INVALID_COMPETITION"),
            "missing_teams": ("EXCLUDE", "MISSING_TEAMS"),
            "missing_start": ("EXCLUDE", "MISSING_START_TIME"),
            "missing_venue": ("EXCLUDE", "MISSING_VENUE"),
            "missing_historical_participants": (
                "EXCLUDE",
                "MISSING_RECONSTRUCTED_PARTICIPANTS",
            ),
            "partial_t45": ("SHADOW_UNRATED", "PARTIAL_T45_PARTICIPANTS"),
            "provenance_mismatch": ("EXCLUDE", "PROVENANCE_MISMATCH"),
            "missing_required_feature": (
                "EXCLUDE",
                "MISSING_REQUIRED_FEATURE_INPUT",
            ),
            "as_of_violation": ("RUN_INVALID", "AS_OF_VIOLATION"),
            "prelock_not_scheduled": ("EXCLUDE", "PRELOCK_NOT_SCHEDULED"),
        }
        for event, expected in cases.items():
            with self.subTest(event=event):
                self.assertEqual(eligibility_disposition(event), expected)

    def test_parser_and_documentation_schema_have_identical_closed_keys(self):
        raw = json.loads(V52_CONFIG.read_text())
        schema = json.loads(V52_SCHEMA.read_text())
        self.assertEqual(set(raw), V52_REQUIRED_KEYS)
        self.assertEqual(set(schema["required"]), V52_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), V52_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        for mutation in (
            lambda value: value.__setitem__("unknown", 1),
            lambda value: value.pop("tier_cascade"),
        ):
            changed = copy.deepcopy(raw)
            mutation(changed)
            with self.assertRaisesRegex(ValueError, "keys"):
                parse_v52_config(changed)

    def test_all_tier_boundaries_and_isolation(self):
        cases = [
            (0.70, 9.0, "Low", False, "S"),
            (0.68, 8.99, "Medium", False, "A"),
            (0.63, 8.5, "Medium", False, "A"),
            (0.58, 8.0, "Medium", False, "B"),
            (0.54, 8.0, "Medium", False, "Lean"),
            (0.539999, 10, "Low", False, "Toss-up"),
            (0.90, 7.999, "High", False, "Toss-up"),
            (0.90, 10, "Low", True, "UNRATED"),
        ]
        for probability, completeness, uncertainty, critical, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_v52(
                        probability, completeness, uncertainty, critical=critical
                    ),
                    expected,
                )
        clipped = {
            "starter": -1,
            "offense": 2,
            "bullpen": 1,
            "environment": 1,
            "defense": 1,
            "schedule": 1,
        }
        self.assertEqual(data_completeness_score(clipped), 6.5)
        key = ranking_key(0.63, 8.5, 4.0, "2025-05-01T17:00:00Z", 1)
        self.assertEqual(key, (-0.63, -8.5, -4.0, "2025-05-01T17:00:00Z", 1))
        self.assertEqual(direction_from_p_home(0.5), (None, 0.5, "NO_DIRECTION_AT_50"))
        self.assertEqual(
            optional_neutral("environment"),
            (50.0, 0.0, "ENV_NEUTRAL_NO_ASOF_SOURCE"),
        )
        self.assertEqual(
            optional_neutral("defense"),
            (50.0, 0.0, "DEF_NEUTRAL_NO_ASOF_SOURCE"),
        )
        qualities = {
            "starter": 0.8,
            "offense": 0.7,
            "bullpen": 0.6,
            "environment": 0,
            "defense": 0,
            "schedule": 1,
        }
        baseline = data_completeness_score(qualities)
        expected_tier = classify_v52(0.63, baseline, "Medium", critical=False)
        expected_rank = ranking_key(
            0.63, baseline, 4, "2025-05-01T17:00:00Z", 1
        )
        for agreement, legacy in ((0, 0), (1, 10), (0.37, 4.2)):
            score = data_completeness_score(
                qualities,
                component_agreement=agreement,
                legacy_selection_quality_score=legacy,
            )
            self.assertEqual(score, baseline)
            self.assertEqual(
                classify_v52(0.63, score, "Medium", critical=False), expected_tier
            )
            self.assertEqual(
                ranking_key(0.63, score, 4, "2025-05-01T17:00:00Z", 1),
                expected_rank,
            )
        self.assertEqual(classify_uncertainty(7.49, 200, False, critical=False), "High")
        self.assertEqual(
            classify_uncertainty(8.99, 200, False, critical=False), "Medium"
        )
        self.assertEqual(
            classify_uncertainty(9.0, 149, False, critical=False), "Medium"
        )
        self.assertEqual(classify_uncertainty(9.0, 150, False, critical=False), "Low")
        self.assertEqual(
            read_legacy_confidence({"confidence": 4.2}),
            {"legacy_selection_quality_score": 4.2},
        )

    def test_schema_mirrors_parser_constants_and_frozen_values(self):
        raw = json.loads(V52_CONFIG.read_text())
        schema = json.loads(V52_SCHEMA.read_text())
        self.assertEqual(schema["const"], raw)
        self.assertEqual(set(schema["required"]), V52_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            parse_v52_config(copy.deepcopy(schema["const"])).raw,
            parse_v52_config(raw).raw,
        )

    def test_every_nested_config_container_is_closed_and_frozen(self):
        raw = json.loads(V52_CONFIG.read_text())
        for path, changed in self._container_mutations(raw):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    parse_v52_config(changed)

    def test_change_contract_is_exact_and_hash_bound(self):
        contract = parse_change_contract(json.loads(CHANGE_CONTRACT.read_text()))
        self.assertEqual(contract["unchanged"], CHANGE_UNCHANGED)
        self.assertEqual(contract["changed"], CHANGE_CHANGED)
        self.assertEqual(contract["forbidden_from_2025_diagnostic"], CHANGE_FORBIDDEN)
        self.assertEqual(contract["config_sha256"], sha256_file(V52_CONFIG))
        self.assertEqual(load_change_contract(ROOT), contract)

    def test_contract_loader_rejects_config_and_design_digest_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            load_change_contract(root, approved_design_root=ROOT)

            config = root / "config/v5_2_qd.json"
            config.write_bytes(config.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "config digest"):
                load_change_contract(root, approved_design_root=ROOT)

            write_v52_config_contracts(root, approved_design_root=ROOT)
            config_value = json.loads(config.read_text())
            config_value["component_weights"]["bullpen"] = 0.19
            config.write_bytes(canonical_json_bytes(config_value) + b"\n")
            contract_path = root / "protocols/v5_2_qd/change_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["config_sha256"] = sha256_file(config)
            contract_path.write_bytes(canonical_json_bytes(contract) + b"\n")
            with self.assertRaisesRegex(ValueError, "frozen value"):
                load_change_contract(root, approved_design_root=ROOT)

            write_v52_config_contracts(root, approved_design_root=ROOT)
            contract_path = root / "protocols/v5_2_qd/change_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["approved_design_blob_sha256"] = "0" * 64
            contract_path.write_bytes(canonical_json_bytes(contract) + b"\n")
            with self.assertRaisesRegex(ValueError, "design digest"):
                load_change_contract(root, approved_design_root=ROOT)

    def test_generated_contracts_are_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root_a, root_b = Path(first), Path(second)
            write_v52_config_contracts(root_a, approved_design_root=ROOT)
            write_v52_config_contracts(root_b, approved_design_root=ROOT)
            paths = (
                "config/v5_2_qd.json",
                "schemas/v5_2_qd.schema.json",
                "protocols/v5_2_qd/change_contract.json",
            )
            for relative in paths:
                self.assertEqual((root_a / relative).read_bytes(), (root_b / relative).read_bytes())

    @staticmethod
    def _container_mutations(raw):
        mutations = []

        def visit(value, path):
            if isinstance(value, dict):
                changed = copy.deepcopy(raw)
                target = changed
                for part in path:
                    target = target[part]
                target["__unknown__"] = None
                mutations.append((f"{path}.unknown", changed))
                for key, child in value.items():
                    visit(child, (*path, key))
            elif isinstance(value, list):
                changed = copy.deepcopy(raw)
                target = changed
                for part in path:
                    target = target[part]
                target.append(copy.deepcopy(value[-1]) if value else None)
                mutations.append((f"{path}.append", changed))
                for index, child in enumerate(value):
                    visit(child, (*path, index))

        visit(raw, ())
        return mutations


if __name__ == "__main__":
    unittest.main()
