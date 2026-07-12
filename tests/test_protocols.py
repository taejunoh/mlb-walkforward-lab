from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import copy
import json
import os
import stat
import tempfile
import unittest

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.protocols import (
    CHANGE_CHANGED,
    CHANGE_FORBIDDEN,
    CHANGE_UNCHANGED,
    INTERNAL_METRIC_LITERALS,
    FOLD_REQUIRED_KEYS,
    FOLD_LITERALS,
    HISTORICAL_DATE_QUEUE,
    HISTORICAL_DENOMINATORS,
    HISTORICAL_OUTCOME_ISOLATION,
    HISTORICAL_SELECTION,
    HISTORICAL_TERMINAL,
    RUN_REQUIRED_KEYS,
    V52_REQUIRED_KEYS,
    classify_uncertainty,
    classify_v52,
    data_completeness_score,
    direction_from_p_home,
    eligibility_disposition,
    expected_v52_config,
    expected_historical_run,
    load_run_spec,
    load_change_contract,
    load_v52_config,
    optional_neutral,
    parse_change_contract,
    parse_run_spec,
    parse_v52_config,
    ranking_key,
    read_legacy_confidence,
    validate_fold,
    validate_run,
    write_historical_contract,
    write_v52_config_contracts,
)


ROOT = Path(__file__).resolve().parents[1]
V52_CONFIG = ROOT / "config/v5_2_qd.json"
V52_SCHEMA = ROOT / "schemas/v5_2_qd.schema.json"
CHANGE_CONTRACT = ROOT / "protocols/v5_2_qd/change_contract.json"
HISTORICAL_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
RUN_SCHEMA = ROOT / "schemas/v5_2_run_spec.schema.json"


class HistoricalRunSpecTest(unittest.TestCase):
    def test_exact_folds_and_oof_sequences(self):
        run = load_run_spec(HISTORICAL_SPEC)
        self.assertEqual(run.target_count, 500)
        self.assertEqual(run.allowed_target_years, (2022, 2023, 2024))
        self.assertEqual(run.forbidden_target_years, (2025,))
        self.assertEqual(run.folds[0].fold_id, "H22")
        self.assertEqual(run.folds[0].normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(
            run.folds[0].oof_calibration,
            (
                (2019, (2018,)),
                (2020, (2018, 2019)),
                (2021, (2018, 2019, 2020)),
            ),
        )
        self.assertEqual(
            run.folds[2].normalization_years,
            (2018, 2019, 2020, 2021, 2022, 2023),
        )

    def test_future_and_2025_injections_fail_closed(self):
        run = load_run_spec(HISTORICAL_SPEC)
        with self.assertRaisesRegex(ValueError, "future season"):
            validate_fold(replace(run.folds[0], normalization_years=(2018, 2022)))
        with self.assertRaisesRegex(ValueError, "2025 is excluded"):
            validate_run(
                replace(
                    run,
                    allowed_target_years=(2022, 2023, 2024, 2025),
                )
            )

    def test_run_and_fold_schema_keys_mirror_strict_parser(self):
        schema = json.loads(RUN_SCHEMA.read_text())
        self.assertEqual(set(schema["required"]), RUN_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), RUN_REQUIRED_KEYS)
        fold_schema = schema["$defs"]["fold"]
        self.assertEqual(set(fold_schema["required"]), FOLD_REQUIRED_KEYS)
        self.assertEqual(set(fold_schema["properties"]), FOLD_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(fold_schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["folds"],
            {"type": "array", "items": {"$ref": "#/$defs/fold"}},
        )

    def test_run_contract_literals(self):
        run = load_run_spec(HISTORICAL_SPEC)
        self.assertEqual(run.raw["date_queue"], HISTORICAL_DATE_QUEUE)
        self.assertEqual(run.raw["selection"], HISTORICAL_SELECTION)
        self.assertEqual(run.raw["terminal_contract"], HISTORICAL_TERMINAL)
        self.assertEqual(
            run.raw["date_state_precedence"],
            ("RUN_INVALID", "SYSTEM_MISS", "NO_ELIGIBLE_DATE", "LOCK_TOP1"),
        )
        self.assertEqual(
            run.raw["evidence_labels"],
            (
                "RUN_INVALID",
                "ROBUSTNESS_NOT_SUPPORTED",
                "ROBUSTNESS_SUPPORTED_NOT_CONFIRMED",
            ),
        )
        self.assertEqual(run.raw["paired_denominators"], HISTORICAL_DENOMINATORS)
        self.assertEqual(run.raw["outcome_isolation"], HISTORICAL_OUTCOME_ISOLATION)

    def test_schema_const_and_parser_reject_every_frozen_change(self):
        raw = json.loads(HISTORICAL_SPEC.read_text())
        schema = json.loads(RUN_SCHEMA.read_text())
        self.assertEqual(schema["const"], raw)
        changed = copy.deepcopy(raw)
        changed["selection"]["graded_target"] = 499
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["folds"][0]["oof_calibration"][0]["normalization_years"] = [2017]
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)

    def test_writer_is_canonical_and_byte_deterministic(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            root_a, root_b = Path(first), Path(second)
            write_historical_contract(root_a)
            write_historical_contract(root_b)
            for relative in (
                "protocols/v5_2_qd/historical_walkforward_500.json",
                "schemas/v5_2_run_spec.schema.json",
            ):
                first_bytes = (root_a / relative).read_bytes()
                self.assertEqual(first_bytes, (root_b / relative).read_bytes())
                self.assertEqual(
                    first_bytes,
                    canonical_json_bytes(json.loads(first_bytes)) + b"\n",
                )

    def test_parser_rejects_unknown_missing_and_type_confusion(self):
        raw = json.loads(HISTORICAL_SPEC.read_text())
        changed = copy.deepcopy(raw)
        changed["unknown"] = None
        with self.assertRaisesRegex(ValueError, "keys"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["folds"][0].pop("latest_training_date")
        with self.assertRaisesRegex(ValueError, "keys"):
            parse_run_spec(changed)
        changed = copy.deepcopy(raw)
        changed["target_count"] = True
        with self.assertRaisesRegex(ValueError, "historical run differs"):
            parse_run_spec(changed)

    def test_fold_literal_oracle_is_recursive_frozen_and_detached(self):
        with self.assertRaises(TypeError):
            FOLD_LITERALS[0]["normalization_years"] = (2017,)
        first = expected_historical_run()
        first["folds"][0]["normalization_years"][0] = 2017
        second = expected_historical_run()
        self.assertEqual(second["folds"][0]["normalization_years"][0], 2018)
        parse_run_spec(second)

    def test_public_fold_validator_rejects_chronology_and_typed_raw_drift(self):
        fold = load_run_spec(HISTORICAL_SPEC).folds[0]
        invalid = (
            (replace(fold, target_dates=("2022-4-07", "2022-10-05")), "canonical ISO"),
            (
                replace(fold, target_dates=("2022-10-05", "2022-04-07")),
                "start must not follow",
            ),
            (replace(fold, target_dates=("2021-04-07", "2022-10-05")), "target year"),
            (replace(fold, latest_training_date="2022-04-07"), "latest training"),
            (
                replace(fold, normalization_years=(2018, 2020, 2019, 2021)),
                "strictly ascending",
            ),
            (
                replace(fold, normalization_years=(2018, 2019, 2020, 2020)),
                "strictly ascending",
            ),
            (replace(fold, normalization_years=(2019, 2020, 2021)), "exact past range"),
            (
                replace(
                    fold,
                    oof_calibration=(
                        (2020, (2018, 2019)),
                        (2019, (2018,)),
                        (2021, (2018, 2019, 2020)),
                    ),
                ),
                "OOF target years",
            ),
            (
                replace(
                    fold,
                    oof_calibration=(
                        (2019, (2018,)),
                        (2020, (2018,)),
                        (2021, (2018, 2019, 2020)),
                    ),
                ),
                "rolling-origin",
            ),
            (replace(fold, fold_id="H99"), "unknown fold"),
        )
        for changed, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_fold(changed)

        raw = copy.deepcopy(json.loads(HISTORICAL_SPEC.read_text())["folds"][0])
        raw["latest_training_date"] = "2020-12-31"
        with self.assertRaisesRegex(ValueError, "raw differs"):
            validate_fold(replace(fold, raw=raw))

    def test_public_run_validator_rejects_duplicate_and_typed_raw_drift(self):
        run = load_run_spec(HISTORICAL_SPEC)
        with self.assertRaisesRegex(ValueError, "fold identity fields are invalid"):
            validate_run(replace(run, folds=(replace(run.folds[0], fold_id=[]),)))
        with self.assertRaisesRegex(ValueError, "fold IDs must be unique"):
            validate_run(replace(run, folds=(run.folds[0], run.folds[0], run.folds[2])))
        duplicate_target = replace(run.folds[1], fold_id="H23-copy", target_year=2022)
        with self.assertRaisesRegex(ValueError, "fold target years must be unique"):
            validate_run(
                replace(run, folds=(run.folds[0], duplicate_target, run.folds[2]))
            )
        with self.assertRaisesRegex(ValueError, "typed fields differ from raw"):
            validate_run(replace(run, target_count=499))

    def test_historical_writer_rolls_back_two_file_bundle(self):
        relative_paths = (
            Path("protocols/v5_2_qd/historical_walkforward_500.json"),
            Path("schemas/v5_2_run_spec.schema.json"),
        )
        for initial_state in ((True, True), (False, False), (True, False)):
            with (
                self.subTest(initial_state=initial_state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                old = {}
                modes = {}
                for index, (relative, preexisting) in enumerate(
                    zip(relative_paths, initial_state, strict=True)
                ):
                    if preexisting:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        old[path] = f"old-{index}".encode()
                        modes[path] = 0o600 + index
                        path.write_bytes(old[path])
                        path.chmod(modes[path])
                original_replace = os.replace
                final_replacements = 0

                def fail_second_final(source, destination):
                    nonlocal final_replacements
                    if Path(destination) in tuple(
                        root / item for item in relative_paths
                    ):
                        final_replacements += 1
                        if final_replacements == 2:
                            raise OSError("second historical replace failed")
                    return original_replace(source, destination)

                with patch(
                    "mlb_backtest.protocols.os.replace", side_effect=fail_second_final
                ):
                    with self.assertRaisesRegex(
                        OSError, "second historical replace failed"
                    ):
                        write_historical_contract(root)
                for relative, preexisting in zip(
                    relative_paths, initial_state, strict=True
                ):
                    path = root / relative
                    if preexisting:
                        self.assertEqual(path.read_bytes(), old[path])
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[path])
                    else:
                        self.assertFalse(path.exists())
                self.assertEqual(list(root.rglob(".*.tmp")), [])

    def test_historical_writer_preserves_modes_and_defaults_new_files_to_0644(self):
        relatives = (
            Path("protocols/v5_2_qd/historical_walkforward_500.json"),
            Path("schemas/v5_2_run_spec.schema.json"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, relative in enumerate(relatives):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old")
                path.chmod(0o600 + index)
            write_historical_contract(root)
            self.assertEqual(
                tuple(stat.S_IMODE((root / item).stat().st_mode) for item in relatives),
                (0o600, 0o601),
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_historical_contract(root)
            self.assertEqual(
                tuple(stat.S_IMODE((root / item).stat().st_mode) for item in relatives),
                (0o644, 0o644),
            )


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

    def test_expected_config_cannot_mutate_the_validation_oracle(self):
        first = expected_v52_config()
        first["internal_metrics"]["starter"]["season_xwoba"]["weight"] = 0.99
        second = expected_v52_config()
        self.assertEqual(
            second["internal_metrics"]["starter"]["season_xwoba"]["weight"],
            0.35,
        )
        self.assertEqual(
            INTERNAL_METRIC_LITERALS["starter"]["season_xwoba"]["weight"],
            0.35,
        )
        with self.assertRaises(TypeError):
            INTERNAL_METRIC_LITERALS["starter"]["season_xwoba"]["weight"] = 0.99
        with self.assertRaisesRegex(ValueError, "frozen value"):
            parse_v52_config(first)
        parse_v52_config(second)

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
        expected_rank = ranking_key(0.63, baseline, 4, "2025-05-01T17:00:00Z", 1)
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

    def test_ranking_timestamp_is_one_canonical_utc_representation(self):
        early = ranking_key(0.63, 8.5, 4, "2025-05-01T07:00:00Z", 2)
        late = ranking_key(0.63, 8.5, 4, "2025-05-01T17:00:00Z", 1)
        self.assertLess(early, late)
        for value in (
            "2025-05-01T17:00:00+00:00",
            "2025-05-01T13:00:00-04:00",
            "2025-05-01T17:00:00",
            "2025-05-01T17:00:00.000Z",
            "2025-5-1T7:00:00Z",
            "2025-02-29T17:00:00Z",
            "not-a-time",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "canonical UTC"):
                    ranking_key(0.63, 8.5, 4, value, 1)

    def test_huge_integer_numeric_inputs_raise_value_error(self):
        huge = 10**10000
        with self.assertRaisesRegex(ValueError, "finite numeric"):
            classify_v52(huge, 8.5, "Medium", critical=False)
        qualities = {
            "starter": huge,
            "offense": 1,
            "bullpen": 1,
            "environment": 1,
            "defense": 1,
            "schedule": 1,
        }
        with self.assertRaisesRegex(ValueError, "finite numeric"):
            data_completeness_score(qualities)

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
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            root_a, root_b = Path(first), Path(second)
            write_v52_config_contracts(root_a, approved_design_root=ROOT)
            write_v52_config_contracts(root_b, approved_design_root=ROOT)
            paths = (
                "config/v5_2_qd.json",
                "schemas/v5_2_qd.schema.json",
                "protocols/v5_2_qd/change_contract.json",
            )
            for relative in paths:
                self.assertEqual(
                    (root_a / relative).read_bytes(), (root_b / relative).read_bytes()
                )

    def test_bundle_resolves_design_before_creating_output_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output"
            bad_design = Path(temporary) / "not-a-repository"
            bad_design.mkdir()
            with self.assertRaisesRegex(ValueError, "approved design blob unavailable"):
                write_v52_config_contracts(root, approved_design_root=bad_design)
            self.assertFalse(root.exists())

    def test_bundle_replace_failure_rolls_back_content_presence_modes_and_temps(self):
        relative_paths = (
            Path("config/v5_2_qd.json"),
            Path("schemas/v5_2_qd.schema.json"),
            Path("protocols/v5_2_qd/change_contract.json"),
        )
        for initial_state in (
            (True, True, True),
            (False, False, False),
            (True, False, True),
        ):
            with (
                self.subTest(initial_state=initial_state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                old = {}
                modes = {}
                for index, (relative, preexisting) in enumerate(
                    zip(relative_paths, initial_state, strict=True)
                ):
                    if preexisting:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        old[path] = f"old-{index}".encode()
                        modes[path] = 0o600 + index
                        path.write_bytes(old[path])
                        path.chmod(modes[path])
                original_replace = os.replace
                final_replacements = 0

                def fail_second_final(source, destination):
                    nonlocal final_replacements
                    if Path(destination) in tuple(
                        root / item for item in relative_paths
                    ):
                        final_replacements += 1
                        if final_replacements == 2:
                            raise OSError("second bundle replace failed")
                    return original_replace(source, destination)

                with patch(
                    "mlb_backtest.protocols.os.replace", side_effect=fail_second_final
                ):
                    with self.assertRaisesRegex(
                        OSError, "second bundle replace failed"
                    ):
                        write_v52_config_contracts(root, approved_design_root=ROOT)
                for relative, preexisting in zip(
                    relative_paths, initial_state, strict=True
                ):
                    path = root / relative
                    if preexisting:
                        self.assertEqual(path.read_bytes(), old[path])
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[path])
                    else:
                        self.assertFalse(path.exists())
                self.assertEqual(list(root.rglob(".*.tmp")), [])

    def test_bundle_success_preserves_existing_modes_and_uses_0644_for_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = (
                root / "config/v5_2_qd.json",
                root / "schemas/v5_2_qd.schema.json",
                root / "protocols/v5_2_qd/change_contract.json",
            )
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old")
                path.chmod(0o600 + index)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            self.assertEqual(
                tuple(stat.S_IMODE(path.stat().st_mode) for path in paths),
                (0o600, 0o601, 0o602),
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_v52_config_contracts(root, approved_design_root=ROOT)
            paths = (
                root / "config/v5_2_qd.json",
                root / "schemas/v5_2_qd.schema.json",
                root / "protocols/v5_2_qd/change_contract.json",
            )
            self.assertEqual(
                tuple(stat.S_IMODE(path.stat().st_mode) for path in paths),
                (0o644, 0o644, 0o644),
            )

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
