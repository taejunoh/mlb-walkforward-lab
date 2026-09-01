import copy
import math
import unittest

from mlb_backtest.model_improvement.contracts import (
    COMPONENTS,
    FrozenModelCard,
    MetricPrimitive,
    PredictionRow,
    PredictionSeal,
    QualityRecipe,
    ScoringModelCard,
    SealedFeatureRow,
    compute_feature_row_sha256,
    sealed_feature_row_payload,
)
from mlb_backtest.model_improvement.lane_disposition import LaneDisposition


SHA = "a" * 64
SHA_B = "b" * 64


def fixed_recipe():
    return QualityRecipe("fixed", "fixed", None, None, None, 0.0, (), (), SHA)


def ratio_recipe():
    return QualityRecipe("ratio", "ratio", "starter", 2.0, 4.0, None, (), (), SHA)


def weighted_recipe():
    return QualityRecipe(
        "weighted",
        "weighted_sum",
        "hitter_split",
        None,
        None,
        None,
        (ratio_recipe(), ratio_recipe()),
        (0.4, 0.6),
        SHA,
    )


def minimum_recipe():
    return QualityRecipe(
        "minimum",
        "minimum",
        None,
        None,
        None,
        None,
        (fixed_recipe(), fixed_recipe()),
        (),
        SHA,
    )


def component_map(value):
    return {
        side: {component: value for component in COMPONENTS}
        for side in ("away", "home")
    }


def primitive_map():
    primitive = MetricPrimitive(50.0, 1.0, fixed_recipe())
    return component_map((primitive,))


def row_values():
    values = {
        "game_pk": 123,
        "official_date": "2024-04-01",
        "scheduled_start_utc": "2024-04-01T19:10:00Z",
        "target_year": 2024,
        "max_history_date": "2024-03-31",
        "source_gate_sha256": SHA,
        "source_manifest_sha256": SHA,
        "exclusion_policy_sha256": SHA,
        "component_scores": component_map(50.0),
        "component_qualities": component_map(0.0),
        "baseline_component_scores": component_map(50.0),
        "baseline_component_qualities": component_map(0.0),
        "metric_primitives": primitive_map(),
        "component_inputs_sha256": component_map(SHA),
        "starter_edge_abs": 0.0,
    }
    digest = compute_feature_row_sha256(values)
    return values | {"feature_row_sha256": digest}


def feature_row():
    return SealedFeatureRow(**row_values())


def scoring_card(
    *,
    model_id="A",
    lane_id="A",
    fold_id="H23",
    candidate_id=None,
    parent_card_sha256=None,
):
    return ScoringModelCard(
        model_id=model_id,
        lane_id=lane_id,
        fold_id=fold_id,
        candidate_id=candidate_id,
        run_spec_sha256=SHA,
        source_gate_sha256=SHA,
        baseline_replay_sha256=SHA,
        parent_card_sha256=parent_card_sha256,
        feature_seal_sha256=SHA,
        prediction_contract_sha256=SHA,
        calibration_card_sha256=SHA,
        candidate_payload_sha256=None,
        parameters={"component_weights_pp": {"starter": 35}, "alpha": [1, {"x": True}]},
        artifact_sha256=SHA_B,
        path="scoring.json",
    )


class QualityRecipeTests(unittest.TestCase):
    def test_recipe_variants_export_as_fresh_json(self):
        for recipe in (
            fixed_recipe(),
            ratio_recipe(),
            weighted_recipe(),
            minimum_recipe(),
        ):
            payload = recipe.as_mapping()
            self.assertEqual(QualityRecipe.from_payload_mapping(payload), recipe)
            payload["children"].append({"mutable": True})
            self.assertEqual(
                len(recipe.children), 0 if recipe.kind in {"fixed", "ratio"} else 2
            )

    def test_fixed_recipe_rejects_value_outside_unit_interval(self):
        with self.assertRaises(ValueError):
            QualityRecipe("x", "fixed", None, None, None, 1.01, (), (), SHA)

    def test_recipe_numeric_values_fail_closed(self):
        for value in (True, math.nan, math.inf, -math.inf, 10**400):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    QualityRecipe("x", "fixed", None, None, None, value, (), (), SHA)

    def test_ratio_recipe_rejects_negative_numerator(self):
        with self.assertRaises(ValueError):
            QualityRecipe("x", "ratio", "starter", -0.01, 1.0, None, (), (), SHA)

    def test_recipe_rejects_invalid_kind_fields_weights_and_family(self):
        with self.assertRaises(ValueError):
            QualityRecipe("x", "fixed", "starter", None, None, 1.0, (), (), SHA)
        with self.assertRaises(ValueError):
            QualityRecipe("x", "ratio", "starter", 1.0, 0.0, None, (), (), SHA)
        with self.assertRaises(ValueError):
            QualityRecipe(
                "x",
                "weighted_sum",
                "starter",
                None,
                None,
                None,
                (ratio_recipe(),),
                (0.0,),
                SHA,
            )
        with self.assertRaises(ValueError):
            QualityRecipe(
                "x", "minimum", "unknown", None, None, None, (fixed_recipe(),), (), SHA
            )


class SealedFeatureRowTests(unittest.TestCase):
    def test_row_hash_projection_round_trips_and_rejects_tampering(self):
        values = row_values()
        row = SealedFeatureRow(**values)
        self.assertNotIn("feature_row_sha256", sealed_feature_row_payload(row))
        self.assertEqual(SealedFeatureRow.from_payload_mapping(row.as_mapping()), row)
        self.assertEqual(
            compute_feature_row_sha256(sealed_feature_row_payload(row)),
            row.feature_row_sha256,
        )
        with self.assertRaises(ValueError):
            sealed_feature_row_payload(sealed_feature_row_payload(row))
        tampered = values | {"starter_edge_abs": 1.0}
        with self.assertRaises(ValueError):
            SealedFeatureRow(**tampered)

    def test_row_validates_complete_side_component_maps_and_numeric_bounds(self):
        values = row_values()
        missing = copy.deepcopy(values)
        del missing["component_scores"]["away"]["starter"]
        missing["feature_row_sha256"] = compute_feature_row_sha256(
            {
                key: value
                for key, value in missing.items()
                if key != "feature_row_sha256"
            }
        )
        with self.assertRaises(ValueError):
            SealedFeatureRow(**missing)
        invalid = row_values()
        invalid["component_qualities"]["home"]["offense"] = math.inf
        with self.assertRaises(ValueError):
            SealedFeatureRow(**invalid)

    def test_row_rejects_quality_and_score_values_outside_their_distinct_ranges(self):
        for field, value in (
            ("component_qualities", 1.0001),
            ("baseline_component_qualities", -0.0001),
            ("component_scores", 100.0001),
            ("baseline_component_scores", -0.0001),
        ):
            with self.subTest(field=field, value=value):
                invalid = row_values()
                invalid[field]["home"]["offense"] = value
                invalid["feature_row_sha256"] = compute_feature_row_sha256(
                    {key: item for key, item in invalid.items() if key != "feature_row_sha256"}
                )
                with self.assertRaises(ValueError):
                    SealedFeatureRow(**invalid)
        for field in ("component_qualities", "baseline_component_qualities"):
            invalid = row_values()
            invalid[field]["home"]["offense"] = True
            invalid["feature_row_sha256"] = compute_feature_row_sha256(
                {key: item for key, item in invalid.items() if key != "feature_row_sha256"}
            )
            with self.assertRaises(ValueError):
                SealedFeatureRow(**invalid)

    def test_row_payload_rejects_malformed_metric_primitive_nesting(self):
        payloads = []
        root_none = feature_row().as_mapping()
        root_none["metric_primitives"] = None
        payloads.append(root_none)
        side_none = feature_row().as_mapping()
        side_none["metric_primitives"]["away"] = None
        payloads.append(side_none)
        collection_none = feature_row().as_mapping()
        collection_none["metric_primitives"]["away"]["starter"] = None
        payloads.append(collection_none)
        collection_scalar = feature_row().as_mapping()
        collection_scalar["metric_primitives"]["away"]["starter"] = 1
        payloads.append(collection_scalar)
        missing_side = feature_row().as_mapping()
        del missing_side["metric_primitives"]["away"]
        payloads.append(missing_side)
        extra_side = feature_row().as_mapping()
        extra_side["metric_primitives"]["neutral"] = {}
        payloads.append(extra_side)
        missing_component = feature_row().as_mapping()
        del missing_component["metric_primitives"]["away"]["starter"]
        payloads.append(missing_component)
        extra_component = feature_row().as_mapping()
        extra_component["metric_primitives"]["away"]["unknown"] = []
        payloads.append(extra_component)
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    SealedFeatureRow.from_payload_mapping(payload)

    def test_row_requires_normalized_metric_weights_and_allows_neutral_fallback(self):
        invalid = row_values()
        invalid["metric_primitives"]["away"]["starter"] = (
            MetricPrimitive(50.0, 0.5, fixed_recipe()),
        )
        invalid["feature_row_sha256"] = compute_feature_row_sha256(
            {
                key: value
                for key, value in invalid.items()
                if key != "feature_row_sha256"
            }
        )
        with self.assertRaises(ValueError):
            SealedFeatureRow(**invalid)
        self.assertEqual(feature_row().component_scores["away"]["environment"], 50.0)
        self.assertEqual(feature_row().component_qualities["away"]["defense"], 0.0)
        non_neutral_fallback = row_values()
        non_neutral_fallback["component_scores"]["away"]["environment"] = 49.0
        non_neutral_fallback["feature_row_sha256"] = compute_feature_row_sha256(
            {
                key: value
                for key, value in non_neutral_fallback.items()
                if key != "feature_row_sha256"
            }
        )
        with self.assertRaises(ValueError):
            SealedFeatureRow(**non_neutral_fallback)

    def test_row_is_recursively_immutable_but_export_is_fresh(self):
        row = feature_row()
        with self.assertRaises(TypeError):
            row.component_scores["away"]["starter"] = 1.0
        payload = row.as_mapping()
        payload["component_scores"]["away"]["starter"] = 1.0
        self.assertEqual(row.component_scores["away"]["starter"], 50.0)


class PredictionAndFrozenCardTests(unittest.TestCase):
    def prediction_row(
        self,
        *,
        p_home=0.6,
        picked_side="home",
        pick_probability=0.6,
        confidence=0.2,
        rank=1,
        prediction_contract_sha256=SHA,
    ):
        return PredictionRow(
            "A", 123, "2024-04-01", 2024, p_home, picked_side, pick_probability,
            confidence, 1.0, "2024-04-01T19:10:00Z", rank, feature_row().feature_row_sha256,
            prediction_contract_sha256, SHA_B,
        )

    def frozen_card(self, *, model_id="A", parent_card_sha256=None, parameters=None):
        return FrozenModelCard(
            model_id=model_id, state="H23_FROZEN", fold_id="H23",
            run_spec_sha256=SHA, source_gate_sha256=SHA,
            baseline_replay_sha256=SHA, parent_card_sha256=parent_card_sha256,
            feature_seal_sha256=SHA, prediction_contract_sha256=SHA,
            scoring_model_card_path="scoring.json", scoring_model_card_sha256=SHA_B,
            prediction_seal_sha256=SHA, evaluation_sha256=SHA,
            parameters={"alpha": [1, {"nested": True}]} if parameters is None else parameters,
            artifact_sha256=SHA_B, path="card.json",
        )

    def test_prediction_row_domains_rank_and_probability(self):
        self.assertEqual(self.prediction_row().picked_side, "home")
        with self.assertRaises(ValueError):
            PredictionRow(
                "A", 123, "2024-04-01", 2024, 1.1, "none", 0.0, 0.0, 0.0,
                "2024-04-01T19:10:00Z", 0, feature_row().feature_row_sha256, SHA, SHA_B,
            )

    def test_prediction_row_confidence_is_a_finite_score_inclusive_of_ten(self):
        for value in (0.0, 10.0):
            self.assertEqual(self.prediction_row(confidence=value).confidence, value)
        for value in (-0.001, 10.001, True, math.nan, math.inf, -math.inf):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.prediction_row(confidence=value)

    def test_prediction_row_requires_pick_direction_and_probability_to_match_p_home(self):
        self.assertEqual(self.prediction_row(), self.prediction_row())
        self.assertEqual(
            self.prediction_row(p_home=0.4, picked_side="away", pick_probability=0.6),
            self.prediction_row(p_home=0.4, picked_side="away", pick_probability=0.6),
        )
        self.assertEqual(
            self.prediction_row(p_home=0.5, picked_side="none", pick_probability=0.5, rank=0),
            self.prediction_row(p_home=0.5, picked_side="none", pick_probability=0.5, rank=0),
        )
        for kwargs in (
            {"p_home": 0.99, "picked_side": "away", "pick_probability": 0.01},
            {"p_home": 0.6, "picked_side": "home", "pick_probability": 0.61},
            {"p_home": 0.6, "picked_side": "home", "pick_probability": 0.60000000005},
            {"p_home": 0.4, "picked_side": "away", "pick_probability": 0.60000000005},
            {"p_home": 0.5, "picked_side": "home", "pick_probability": 0.5},
            {"p_home": 0.5, "picked_side": "none", "pick_probability": 0.49, "rank": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.prediction_row(**kwargs)
        payload = self.prediction_row().as_mapping()
        payload["picked_side"] = "away"
        with self.assertRaises(ValueError):
            PredictionRow.from_payload_mapping(payload)
        with self.assertRaises(ValueError):
            PredictionRow(
                "A", 123, "2024-04-01", 2024, 0.5, "home", 0.5, 0.0, 0.0,
                "2024-04-01T19:10:00Z", 0, feature_row().feature_row_sha256, SHA, SHA_B,
            )

    def test_prediction_payload_rejects_legacy_model_card_key_and_bad_contract_digest(self):
        row = self.prediction_row()
        payload = row.as_mapping()
        self.assertEqual(PredictionRow.from_payload_mapping(payload), row)
        payload["model_card_sha256"] = payload.pop("scoring_model_card_sha256")
        with self.assertRaises(ValueError):
            PredictionRow.from_payload_mapping(payload)
        with self.assertRaises(ValueError):
            self.prediction_row(prediction_contract_sha256="A" * 64)

    def test_prediction_seal_descriptor_free_payload_round_trip(self):
        seal = PredictionSeal(
            "lane_a/A/H23", "H23", "A", None, SHA, SHA, SHA, SHA_B, SHA,
            None, SHA, SHA_B, "predictions.json",
        )
        payload = seal.as_mapping()
        self.assertNotIn("artifact_sha256", payload)
        self.assertNotIn("path", payload)
        self.assertEqual(
            PredictionSeal.from_payload_mapping(payload, SHA_B, "predictions.json"),
            seal,
        )
        payload["model_card_sha256"] = payload.pop("scoring_model_card_sha256")
        with self.assertRaises(ValueError):
            PredictionSeal.from_payload_mapping(payload, SHA_B, "predictions.json")

    def test_prediction_seal_final_card_binding_is_fold_and_model_specific(self):
        with self.assertRaises(ValueError):
            PredictionSeal(
                "lane_a/A/O19", "O19", "A", None, SHA, SHA, SHA, SHA_B, SHA,
                SHA, SHA, SHA_B, "predictions.json",
            )
        with self.assertRaises(ValueError):
            PredictionSeal(
                "baseline/v5.1/H24", "H24", "v5.1", None, SHA, SHA, SHA, SHA_B,
                SHA, SHA, SHA, SHA_B, "predictions.json",
            )
        with self.assertRaises(ValueError):
            PredictionSeal(
                "h24/A/H24", "H24", "A", None, SHA, SHA, SHA, SHA_B, SHA,
                None, SHA, SHA_B, "predictions.json",
            )
        valid = PredictionSeal(
            "h24/A/H24", "H24", "A", None, SHA, SHA, SHA, SHA_B, SHA,
            SHA, SHA, SHA_B, "predictions.json",
        )
        self.assertEqual(valid.frozen_model_card_sha256, SHA)

    def test_scoring_card_round_trip_is_pre_prediction_and_recursively_immutable(self):
        card = scoring_card()
        payload = card.as_mapping()
        self.assertNotIn("artifact_sha256", payload)
        self.assertNotIn("path", payload)
        self.assertNotIn("prediction_seal_sha256", payload)
        self.assertNotIn("evaluation_sha256", payload)
        self.assertEqual(
            ScoringModelCard.from_payload_mapping(payload, SHA_B, "scoring.json"), card
        )
        with self.assertRaises(TypeError):
            card.parameters["alpha"] = 0
        exported = card.as_mapping()
        exported["parameters"]["alpha"][1]["x"] = False
        self.assertTrue(card.parameters["alpha"][1]["x"])

    def test_scoring_card_rejects_baseline_and_incompatible_lane_fold_candidate(self):
        for kwargs in (
            {"model_id": "v5.1", "lane_id": "A"},
            {"model_id": "A+B", "lane_id": "A", "candidate_id": "candidate"},
            {"model_id": "A+B", "lane_id": "B", "candidate_id": None},
            {"model_id": "A", "lane_id": "A", "fold_id": "O19"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    scoring_card(**kwargs)

    def test_scoring_card_requires_the_declared_parent_for_h24_and_lane_c(self):
        for kwargs in (
            {"fold_id": "H24"},
            {"model_id": "A+B+C", "lane_id": "C", "fold_id": "H22"},
            {"model_id": "A+B+C", "lane_id": "C", "fold_id": "H23"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    scoring_card(**kwargs)
        self.assertEqual(
            scoring_card(fold_id="H24", parent_card_sha256=SHA).parent_card_sha256,
            SHA,
        )
        self.assertEqual(
            scoring_card(
                model_id="A+B+C", lane_id="C", fold_id="H23", parent_card_sha256=SHA
            ).parent_card_sha256,
            SHA,
        )

    def test_scoring_card_validates_optional_and_lane_b_candidate_digests(self):
        lane_b = ScoringModelCard(
            model_id="A+B", lane_id="B", fold_id="H22", candidate_id="b-s35",
            run_spec_sha256=SHA, source_gate_sha256=SHA, baseline_replay_sha256=SHA,
            parent_card_sha256=SHA, feature_seal_sha256=SHA,
            prediction_contract_sha256=SHA, calibration_card_sha256=None,
            candidate_payload_sha256=SHA, parameters={"component_weights_pp": {}},
            artifact_sha256=SHA_B, path="lane-b-scoring.json",
        )
        self.assertEqual(
            ScoringModelCard.from_payload_mapping(
                lane_b.as_mapping(), SHA_B, "lane-b-scoring.json"
            ),
            lane_b,
        )
        with self.assertRaises(ValueError):
            ScoringModelCard(
                model_id="A", lane_id="A", fold_id="H23", candidate_id=None,
                run_spec_sha256=SHA, source_gate_sha256=SHA, baseline_replay_sha256=SHA,
                parent_card_sha256="bad", feature_seal_sha256=SHA,
                prediction_contract_sha256=SHA, calibration_card_sha256=None,
                candidate_payload_sha256=None, parameters={}, artifact_sha256=SHA_B,
                path="scoring.json",
            )

    def test_final_card_points_back_to_pre_prediction_scoring_card_without_cycle(self):
        scoring = scoring_card()
        seal = PredictionSeal(
            "lane_a/A/H23", "H23", "A", None, SHA, SHA, SHA,
            scoring.artifact_sha256, SHA, None, SHA, SHA_B, "predictions.json",
        )
        final = self.frozen_card()
        self.assertIsNone(seal.frozen_model_card_sha256)
        self.assertEqual(final.scoring_model_card_sha256, scoring.artifact_sha256)
        self.assertNotIn("prediction_seal_sha256", scoring.as_mapping())

    def test_frozen_card_payload_is_closed_and_parameters_are_immutable(self):
        card = self.frozen_card()
        self.assertEqual(
            FrozenModelCard.from_payload_mapping(card.as_mapping(), SHA_B, "card.json"), card
        )
        with self.assertRaises(TypeError):
            card.parameters["alpha"] = 1
        exported = card.as_mapping()
        exported["parameters"]["alpha"][1]["nested"] = False
        self.assertTrue(card.parameters["alpha"][1]["nested"])
        with self.assertRaises(ValueError):
            FrozenModelCard(
                model_id="v5.1", state="H23_FROZEN", fold_id="H23",
                run_spec_sha256=SHA, source_gate_sha256=SHA, baseline_replay_sha256=SHA,
                parent_card_sha256=None, feature_seal_sha256=SHA,
                prediction_contract_sha256=SHA, scoring_model_card_path="scoring.json",
                scoring_model_card_sha256=SHA, prediction_seal_sha256=SHA,
                evaluation_sha256=SHA, parameters={}, artifact_sha256=SHA_B, path="card.json",
            )

    def test_frozen_card_requires_the_model_parent_hierarchy(self):
        with self.assertRaises(ValueError):
            self.frozen_card(parent_card_sha256=SHA)
        for model_id in ("A+B", "A+B+C"):
            with self.subTest(model_id=model_id):
                with self.assertRaises(ValueError):
                    self.frozen_card(model_id=model_id)
                self.assertEqual(
                    self.frozen_card(model_id=model_id, parent_card_sha256=SHA).parent_card_sha256,
                    SHA,
                )

    def test_frozen_card_rejects_cyclic_json_parameters(self):
        cyclic_mapping = {}
        cyclic_mapping["self"] = cyclic_mapping
        cyclic_sequence = []
        cyclic_sequence.append(cyclic_sequence)
        for parameters in (cyclic_mapping, {"loop": cyclic_sequence}):
            with self.subTest(parameters=type(parameters).__name__):
                with self.assertRaises(ValueError):
                    self.frozen_card(parameters=parameters)


class LaneDispositionTests(unittest.TestCase):
    def test_lane_disposition_payload_round_trip_and_domains(self):
        disposition = LaneDisposition(
            "A",
            "QUALIFIED",
            SHA,
            None,
            (SHA,),
            ("ENOUGH_EVIDENCE",),
            SHA_B,
            "lane.json",
        )
        self.assertEqual(
            LaneDisposition.from_payload_mapping(
                disposition.as_mapping(), SHA_B, "lane.json"
            ),
            disposition,
        )
        self.assertEqual(
            set(disposition.as_mapping()),
            {
                "lane_id",
                "state",
                "run_spec_sha256",
                "parent_card_sha256",
                "evidence_sha256",
                "reason_codes",
            },
        )
        with self.assertRaises(ValueError):
            LaneDisposition(
                "Z",
                "QUALIFIED",
                SHA,
                None,
                (SHA,),
                ("ENOUGH_EVIDENCE",),
                SHA_B,
                "lane.json",
            )
        with self.assertRaises(ValueError):
            LaneDisposition("A", "QUALIFIED", SHA, None, (SHA,), (), SHA_B, "lane.json")


if __name__ == "__main__":
    unittest.main()
