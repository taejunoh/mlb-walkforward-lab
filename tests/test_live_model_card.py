import copy
import hashlib
import json
import math
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from mlb_backtest.scoring import WEIGHTS
from mlb_backtest.scoring_prep import score_raw_game
from tests.test_scoring_prep import _raw_game


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "audit" / "v5_1_bt" / "model_card.json"
EXPECTED = "9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759"


def _payload_with(**changes):
    payload = json.loads(CARD.read_text(encoding="utf-8"))
    payload.update(changes)
    return payload


def _write_payload(directory: Path, payload: object) -> tuple[Path, str]:
    path = directory / "card.json"
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class LiveModelCardTests(unittest.TestCase):
    def test_loads_exact_frozen_projection_without_historical_date_limit(self):
        from mlb_backtest.live_model_card import load_frozen_live_card

        card = load_frozen_live_card(CARD)

        self.assertEqual(card.file_sha256, EXPECTED)
        self.assertEqual(card.model_version, "v5.1-BT")
        self.assertEqual(dict(card.component_weights), dict(WEIGHTS))
        self.assertEqual(set(card.slot_weights), set(range(1, 10)))
        self.assertTrue(all(math.isfinite(value) for value in card.slot_weights.values()))
        self.assertAlmostEqual(math.fsum(card.slot_weights.values()), 1.0)
        self.assertTrue(math.isfinite(card.alpha))
        self.assertTrue(math.isfinite(card.beta))
        self.assertGreater(card.beta, 0.0)
        self.assertEqual(card.normalization["starter"]["fip"]["direction"], -1)

    def test_rejects_one_byte_tampering_before_projection(self):
        from mlb_backtest.live_model_card import load_frozen_live_card

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "card.json"
            path.write_bytes(
                CARD.read_bytes().replace(b'"beta": 0.612', b'"beta": 0.613')
            )
            with self.assertRaisesRegex(ValueError, "digest"):
                load_frozen_live_card(path)

    def test_does_not_apply_historical_training_date_limits(self):
        from mlb_backtest import live_model_card

        payload = _payload_with()
        payload["training_window"] = {
            "actual_calibration_min_date": "2026-01-01",
            "actual_feature_min_date": "2026-01-01",
            "actual_max_date": "2026-12-31",
            "configured_end": "2026-12-31",
            "configured_start": "2026-01-01",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = _write_payload(Path(temporary), payload)
            with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                card = live_model_card.load_frozen_live_card(path)
        self.assertEqual(card.model_version, "v5.1-BT")

    def test_rejects_wrong_model_version_after_digest_is_authenticated(self):
        from mlb_backtest import live_model_card

        payload = _payload_with(model_version="other")
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = _write_payload(Path(temporary), payload)
            with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                with self.assertRaisesRegex(ValueError, "version"):
                    live_model_card.load_frozen_live_card(path)

    def test_returned_mappings_are_deeply_immutable(self):
        from mlb_backtest.live_model_card import load_frozen_live_card

        card = load_frozen_live_card(CARD)

        with self.assertRaises(TypeError):
            card.slot_weights[1] = 0.0
        with self.assertRaises(TypeError):
            card.component_weights["starter"] = 0.0
        with self.assertRaises(TypeError):
            card.normalization["starter"]["fip"]["mean"] = 0.0

    def test_rejects_invalid_normalization_after_digest_is_authenticated(self):
        from mlb_backtest import live_model_card

        payload = _payload_with()
        payload["normalization"]["starter"]["fip"]["sd"] = 0.0
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = _write_payload(Path(temporary), payload)
            with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                with self.assertRaisesRegex(ValueError, "normalization"):
                    live_model_card.load_frozen_live_card(path)

    def test_rejects_nonexact_normalization_numeric_leaves_after_authentication(self):
        from mlb_backtest import live_model_card

        cases = (
            ("direction", True),
            ("direction", 1.0),
            ("mean", True),
            ("mean", float("inf")),
            ("sd", True),
            ("sd", float("nan")),
            ("n", True),
            ("n", 14530.0),
            ("n", 0),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                payload = _payload_with()
                payload["normalization"]["starter"]["expected_ip"][field] = value
                path, digest = _write_payload(Path(temporary), payload)
                with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                    with self.assertRaisesRegex(ValueError, "normalization"):
                        live_model_card.load_frozen_live_card(path)

    def test_rejects_nonfinite_or_nonpositive_calibration_after_digest_is_authenticated(self):
        from mlb_backtest import live_model_card

        for field, value in (("alpha", float("nan")), ("beta", float("inf")), ("beta", 0.0)):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                payload = _payload_with()
                payload["calibration"][field] = value
                path, digest = _write_payload(Path(temporary), payload)
                with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                    with self.assertRaisesRegex(ValueError, "calibration"):
                        live_model_card.load_frozen_live_card(path)

    def test_rejects_nonfinite_missing_or_nonunit_slot_weights_after_digest_is_authenticated(self):
        from mlb_backtest import live_model_card

        cases = (
            ({"1": float("nan")}, "slot weights"),
            ({"10": 0.1}, "slot weights"),
            ({"1": 0.2}, "slot weights"),
        )
        for replacement, message in cases:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                payload = _payload_with()
                payload["slot_weights"].update(replacement)
                path, digest = _write_payload(Path(temporary), payload)
                with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                    with self.assertRaisesRegex(ValueError, message):
                        live_model_card.load_frozen_live_card(path)

    def test_component_weights_must_equal_current_scoring_constants(self):
        from mlb_backtest import live_model_card

        payload = _payload_with()
        for component in WEIGHTS:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                changed = copy.deepcopy(payload)
                changed["component_weights"][component] += 0.01
                path, digest = _write_payload(Path(temporary), changed)
                with patch.object(live_model_card, "EXPECTED_MODEL_CARD_SHA256", digest):
                    with self.assertRaisesRegex(ValueError, "component weights"):
                        live_model_card.load_frozen_live_card(path)

        with patch.object(live_model_card, "SCORING_WEIGHTS", MappingProxyType({"starter": 1.0})):
            with self.assertRaisesRegex(ValueError, "component weights"):
                live_model_card.load_frozen_live_card(CARD)

    def test_live_card_normalization_has_scoring_regression_parity(self):
        from mlb_backtest.live_model_card import load_frozen_live_card

        payload = json.loads(CARD.read_text(encoding="utf-8"))
        card = load_frozen_live_card(CARD)
        raw_game = _raw_game(999, "2026-07-21", shift=0.01)

        historical = score_raw_game(raw_game, payload["normalization"])
        live = score_raw_game(raw_game, card.normalization)

        self.assertEqual(
            _canonical(_thaw(card.normalization)),
            _canonical(payload["normalization"]),
        )
        self.assertEqual(live, historical)


if __name__ == "__main__":
    unittest.main()
