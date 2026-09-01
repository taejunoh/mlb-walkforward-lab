import copy
import json
import math
import unittest

from mlb_backtest.model_improvement.contracts import (
    COMPONENTS,
    FrozenModelCard,
    MetricPrimitive,
    PredictionRow,
    PredictionSeal,
    QualityRecipe,
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
    def test_prediction_row_domains_rank_and_probability(self):
        row = feature_row()
        prediction = PredictionRow(
            "A",
            123,
            "2024-04-01",
            2024,
            0.6,
            "home",
            0.6,
            0.2,
            1.0,
            "2024-04-01T19:10:00Z",
            1,
            row.feature_row_sha256,
            SHA,
        )
        self.assertEqual(prediction.picked_side, "home")
        with self.assertRaises(ValueError):
            PredictionRow(
                "A",
                123,
                "2024-04-01",
                2024,
                1.1,
                "none",
                0.0,
                0.0,
                0.0,
                "2024-04-01T19:10:00Z",
                0,
                row.feature_row_sha256,
                SHA,
            )
        with self.assertRaises(ValueError):
            PredictionRow(
                "A",
                123,
                "2024-04-01",
                2024,
                0.5,
                "home",
                0.5,
                0.0,
                0.0,
                "2024-04-01T19:10:00Z",
                0,
                row.feature_row_sha256,
                SHA,
            )

    def test_prediction_seal_descriptor_free_payload_round_trip(self):
        seal = PredictionSeal(
            "H23", SHA, SHA, SHA, SHA, SHA, SHA, SHA_B, "predictions.json"
        )
        payload = seal.as_mapping()
        self.assertNotIn("artifact_sha256", payload)
        self.assertNotIn("path", payload)
        self.assertEqual(
            PredictionSeal.from_payload_mapping(payload, SHA_B, "predictions.json"),
            seal,
        )

    def test_frozen_card_payload_is_closed_and_parameters_are_immutable(self):
        card = FrozenModelCard(
            "A",
            "H23_FROZEN",
            "H23",
            SHA,
            SHA,
            SHA,
            None,
            SHA,
            SHA,
            SHA,
            SHA,
            {"alpha": [1, {"nested": True}]},
            SHA_B,
            "card.json",
        )
        self.assertEqual(
            FrozenModelCard.from_payload_mapping(card.as_mapping(), SHA_B, "card.json"),
            card,
        )
        with self.assertRaises(TypeError):
            card.parameters["alpha"] = 1
        exported = card.as_mapping()
        exported["parameters"]["alpha"][1]["nested"] = False
        self.assertTrue(card.parameters["alpha"][1]["nested"])
        with self.assertRaises(ValueError):
            FrozenModelCard(
                "v5.1",
                "H23_FROZEN",
                "H23",
                SHA,
                SHA,
                SHA,
                None,
                SHA,
                SHA,
                SHA,
                SHA,
                {},
                SHA_B,
                "card.json",
            )


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
        self.assertNotIn("artifact_sha256", disposition.as_mapping())
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


class LaneDispositionSchemaTests(unittest.TestCase):
    def test_schema_is_closed_and_descriptor_free(self):
        with open(
            "schemas/model_improvement/lane_disposition.schema.json", encoding="utf-8"
        ) as handle:
            schema = json.load(handle)
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("artifact_sha256", schema["properties"])
        self.assertNotIn("path", schema["properties"])
        self.assertEqual(schema["properties"]["lane_id"]["enum"], ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
