import copy
import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.config import load_config


CONFIG_PATH = Path(__file__).parents[1] / "config" / "v5_1_bt.json"


class ConfigTest(unittest.TestCase):
    def test_loads_frozen_v5_1_backtest_model_card(self):
        config = load_config(CONFIG_PATH)

        self.assertEqual(config["model_version"], "v5.1-BT")
        self.assertEqual(config["protocol"], "Backtest")
        self.assertEqual(
            config["training_window"],
            {"start": "2022-01-01", "end": "2024-12-31"},
        )
        self.assertEqual(
            config["initial_window"],
            {"start": "2025-05-01", "end": "2025-08-12"},
        )
        self.assertEqual(config["graded_target"], 100)
        self.assertEqual(config["lock_minutes_before_first_pitch"], 45)
        self.assertEqual(
            config["component_weights"],
            {
                "starter": 0.35,
                "offense": 0.25,
                "bullpen": 0.20,
                "environment": 0.10,
                "defense": 0.05,
                "schedule": 0.05,
            },
        )
        self.assertAlmostEqual(sum(config["component_weights"].values()), 1.0)
        self.assertEqual(
            config["shrinkage_constants"],
            {
                "starter": 150,
                "hitter_split": 150,
                "recent_form": 100,
                "bullpen": 300,
                "defense_catcher": 500,
            },
        )
        self.assertEqual(config["probability_clip"], [0.10, 0.90])
        self.assertEqual(
            config["ranking"],
            [
                "predicted_winner_probability_desc",
                "confidence_desc",
                "absolute_starter_edge_desc",
                "scheduled_start_utc_asc",
                "gamePk_asc",
            ],
        )

    def test_rejects_changes_to_frozen_fields(self):
        valid = json.loads(CONFIG_PATH.read_text())
        mutations = {
            "components": lambda c: c["component_weights"].pop("schedule"),
            "weight sum": lambda c: c["component_weights"].update(starter=0.36),
            "model version": lambda c: c.update(model_version="v5.2-BT"),
            "protocol": lambda c: c.update(protocol="Live"),
            "lock": lambda c: c.update(lock_minutes_before_first_pitch=30),
            "training start": lambda c: c["training_window"].update(start="2023-01-01"),
            "training end": lambda c: c["training_window"].update(end="2025-01-01"),
            "initial start": lambda c: c["initial_window"].update(start="2025-05-02"),
            "initial end": lambda c: c["initial_window"].update(end="2025-08-13"),
            "graded target": lambda c: c.update(graded_target=99),
            "probability clip": lambda c: c.update(probability_clip=[0.05, 0.95]),
            "ranking": lambda c: c["ranking"].reverse(),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                changed = copy.deepcopy(valid)
                mutate(changed)
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
