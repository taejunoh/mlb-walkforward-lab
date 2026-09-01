"""Admission tests for immutable, pre-target calibration authority."""

from __future__ import annotations

import unittest
import json
import tempfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.run_spec import (
    AbsolutePathDigest,
    PathDigest,
    VerifiedRunSpecActivation,
    build_run_spec,
    load_run_spec,
    run_spec_sha256,
)


SPEC_PATH = (
    Path(__file__).parent
    / "fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ProbabilityCalibrationPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_run_spec(SPEC_PATH)

    def test_policy_is_closed_and_has_all_seven_distinct_fold_keys(self) -> None:
        policy = self.spec.probability_calibration_policy
        self.assertEqual(
            dict(policy.fold_keys),
            {
                "O19": "C19",
                "O20": "C20",
                "O21": "C21",
                "O22": "C22O",
                "H22": "C22H",
                "O23": "C23O",
                "H23": "C23H",
            },
        )
        self.assertEqual(policy.pools["C22O"].training_end, "2021-12-31")
        self.assertEqual(policy.pools["C22H"].training_end, "2021-12-31")
        self.assertEqual(policy.pools["C23O"].target_year, 2023)
        self.assertEqual(policy.pools["C23H"].target_year, 2023)
        self.assertEqual(
            policy.fit_spec.as_mapping(),
            {
                "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1",
                "algorithm": "fit_logistic_v1",
                "edge_divisor": 10.0,
                "iterations": 100,
                "l2": 1e-6,
                "tolerance": 1e-10,
                "min_rows": 2,
                "required_classes": [0, 1],
            },
        )

    def test_calibration_namespace_rejects_noncanonical_shapes(self) -> None:
        self.assertEqual(
            self.spec.output_path(
                "calibration", "baseline/v5.1/O19/v2/calibration_rows.jsonl"
            ).name,
            "calibration_rows.jsonl",
        )
        self.assertEqual(
            self.spec.output_path(
                "calibration", "baseline/v5.1/O19/v2/probability_calibration_card.json"
            ).name,
            "probability_calibration_card.json",
        )
        for relative in (
            "baseline/v5.1/O19/v2/unknown.json",
            "baseline/v5.1/H24/v2/calibration_rows.jsonl",
            "A/_none/O19/calibration_rows.jsonl",
            "lane_a/candidate/O19/calibration_rows.jsonl",
            "B/../O19/calibration_rows.jsonl",
            "B//candidate/O19/calibration_rows.jsonl",
        ):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.spec.output_path("calibration", relative)


class ProbabilityCalibrationAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_run_spec(SPEC_PATH)

    def test_identity_and_paths_are_derived_not_caller_selected(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            calibration_paths_v2,
        )

        paths = calibration_paths_v2(self.spec, ("v5.1", "baseline", "O19"))
        self.assertTrue(
            str(paths.rows).endswith(
                "calibration/baseline/v5.1/O19/v2/calibration_rows.jsonl"
            )
        )
        self.assertTrue(
            str(paths.card).endswith(
                "calibration/baseline/v5.1/O19/v2/probability_calibration_card.json"
            )
        )
        self.assertTrue(
            str(paths.rows_sidecar).endswith("calibration_rows.jsonl.sha256")
        )
        self.assertTrue(
            str(paths.card_sidecar).endswith("probability_calibration_card.json.sha256")
        )
        for identity in (
            ("v5.1", "A", "O19", None),
            ("A+B", "B", "O19", None),
            ("A+B+C", "C", "O19", "candidate"),
            ("H24", "baseline", "H24", None),
        ):
            with self.subTest(identity=identity), self.assertRaises(RuntimeError):
                calibration_paths_v2(self.spec, identity)

    def test_missing_authority_blocks_before_prediction_output_access(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            CalibrationAuthorityBlockedError,
            load_probability_calibration_card_v2,
        )

        with self.assertRaises(CalibrationAuthorityBlockedError) as raised:
            load_probability_calibration_card_v2(self.spec, None, "O19")
        self.assertEqual(raised.exception.code, "BLOCKED_CALIBRATION_AUTHORITY_MISSING")
        self.assertEqual(str(raised.exception), "BLOCKED_CALIBRATION_AUTHORITY_MISSING")

    def test_card_payload_is_closed_and_rejects_nonfinite_coefficients(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            ProbabilityCalibrationCard,
        )

        digest = "a" * 64
        payload = {
            "model_id": "v5.1",
            "lane_id": "baseline",
            "fold_id": "O19",
            "candidate_id": None,
            "calibration_key": "C19",
            "target_year": 2019,
            "training_start": "2018-01-01",
            "training_end": "2018-12-31",
            "run_spec_sha256": digest,
            "source_gate_sha256": digest,
            "prediction_contract_sha256": digest,
            "weight_authority_path": "/tmp/weights.json",
            "weight_authority_sha256": digest,
            "calibration_rows_path": "/tmp/calibration_rows.jsonl",
            "calibration_rows_sha256": digest,
            "calibration_population_sha256": digest,
            "game_evidence_inventory_path": "/tmp/inventory.json",
            "game_evidence_inventory_sha256": digest,
            "fit_spec": self.spec.probability_calibration_policy.fit_spec.as_mapping(),
            "alpha": 0.0,
            "beta": 1.0,
        }
        card = ProbabilityCalibrationCard.from_payload_mapping(
            payload, digest, "/tmp/card.json"
        )
        self.assertEqual(card.as_mapping(), payload)
        with self.assertRaises(ValueError):
            ProbabilityCalibrationCard.from_payload_mapping(
                payload | {"beta": float("inf")}, digest, "/tmp/card.json"
            )
        with self.assertRaises(ValueError):
            ProbabilityCalibrationCard.from_payload_mapping(
                payload | {"unknown": True}, digest, "/tmp/card.json"
            )

    def _published_baseline_authority(self, root: Path):
        from mlb_backtest.model_improvement.probability_calibration import (
            calibration_paths,
        )
        from mlb_backtest.normalize import fit_logistic

        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        raw["outputs"]["root"] = str(root / "outputs")
        spec = build_run_spec(raw)
        paths = calibration_paths(spec, "v5.1", "baseline", "O19", None)
        paths.rows.parent.mkdir(parents=True)
        source_gate = spec.output_path("source_gate", "source_gate_receipt.json")
        source_gate.parent.mkdir(parents=True)
        source_gate.write_bytes(canonical_json_bytes({"source": "synthetic"}))
        evidence = root / "evidence.json"
        evidence.write_bytes(canonical_json_bytes({"inventory": "synthetic"}))
        rows = [
            {
                "game_pk": 1,
                "official_date": "2018-04-01",
                "target_year": 2019,
                "edge": -2.0,
                "home_win": 0,
                "feature_provenance_sha256": "1" * 64,
                "game_evidence_sha256": "2" * 64,
            },
            {
                "game_pk": 2,
                "official_date": "2018-05-01",
                "target_year": 2019,
                "edge": -1.0,
                "home_win": 0,
                "feature_provenance_sha256": "3" * 64,
                "game_evidence_sha256": "4" * 64,
            },
            {
                "game_pk": 3,
                "official_date": "2018-06-01",
                "target_year": 2019,
                "edge": 1.0,
                "home_win": 1,
                "feature_provenance_sha256": "5" * 64,
                "game_evidence_sha256": "6" * 64,
            },
            {
                "game_pk": 4,
                "official_date": "2018-07-01",
                "target_year": 2019,
                "edge": 2.0,
                "home_win": 1,
                "feature_provenance_sha256": "7" * 64,
                "game_evidence_sha256": "8" * 64,
            },
        ]
        row_bytes = b"".join(canonical_json_bytes(row) for row in rows)
        alpha, beta = fit_logistic(
            (row["edge"] / 10.0 for row in rows),
            (row["home_win"] for row in rows),
            iterations=100,
            l2=1e-6,
            tolerance=1e-10,
        )
        weight = REPOSITORY_ROOT / "tests/fixtures/model_improvement/v51_card.json"
        payload = {
            "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1",
            "model_id": "v5.1",
            "lane_id": "baseline",
            "fold_id": "O19",
            "candidate_id": None,
            "calibration_key": "C19",
            "target_year": 2019,
            "training_start": "2018-01-01",
            "training_end": "2018-12-31",
            "run_spec_sha256": run_spec_sha256(spec),
            "source_gate_sha256": sha256(source_gate.read_bytes()).hexdigest(),
            "prediction_contract_sha256": spec.prediction_contract_sha256,
            "weight_authority_path": str(weight),
            "weight_authority_sha256": sha256(weight.read_bytes()).hexdigest(),
            "calibration_rows_path": str(paths.rows),
            "calibration_rows_sha256": sha256(row_bytes).hexdigest(),
            "calibration_population_sha256": sha256(
                canonical_json_bytes([1, 2, 3, 4])
            ).hexdigest(),
            "game_evidence_inventory_path": str(evidence),
            "game_evidence_inventory_sha256": sha256(evidence.read_bytes()).hexdigest(),
            "fit_spec": spec.probability_calibration_policy.fit_spec.as_mapping(),
            "alpha": alpha,
            "beta": beta,
        }
        card_bytes = canonical_json_bytes(payload)
        paths.rows.write_bytes(row_bytes)
        paths.rows_sidecar.write_bytes(
            canonical_json_bytes({"artifact_sha256": sha256(row_bytes).hexdigest()})
        )
        paths.card_sidecar.write_bytes(
            canonical_json_bytes({"artifact_sha256": sha256(card_bytes).hexdigest()})
        )
        paths.card.write_bytes(card_bytes)
        return spec, paths, payload, rows

    def test_load_recomputes_and_rereads_exact_published_authority(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            load_probability_calibration_card,
        )

        with self.assertRaisesRegex(ValueError, "inspection-only"):
            load_probability_calibration_card(
                self.spec, "v5.1", "baseline", "O19", None
            )
        return

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, paths, payload, _ = self._published_baseline_authority(
                Path(directory)
            )
            card = load_probability_calibration_card(
                spec, "v5.1", "baseline", "O19", None
            )
            self.assertEqual(
                card.as_mapping(),
                {key: value for key, value in payload.items() if key != "schema"},
            )
            self.assertEqual(card.path, str(paths.card))

    def test_present_authority_rejects_rows_and_card_mutations(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            load_probability_calibration_card,
        )

        with self.assertRaisesRegex(ValueError, "inspection-only"):
            load_probability_calibration_card(
                self.spec, "v5.1", "baseline", "O19", None
            )
        return

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, paths, payload, rows = self._published_baseline_authority(
                Path(directory)
            )
            attacks = {
                "fit_spec": payload | {"fit_spec": payload["fit_spec"] | {"l2": 0.1}},
                "alpha": payload | {"alpha": payload["alpha"] + 0.01},
                "key": payload | {"calibration_key": "C20"},
                "path": payload
                | {"calibration_rows_path": str(paths.rows.parent / "other.jsonl")},
            }
            for name, attack in attacks.items():
                with self.subTest(name=name):
                    raw = canonical_json_bytes(attack)
                    paths.card_sidecar.write_bytes(
                        canonical_json_bytes(
                            {"artifact_sha256": sha256(raw).hexdigest()}
                        )
                    )
                    paths.card.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        load_probability_calibration_card(
                            spec, "v5.1", "baseline", "O19", None
                        )
            # Restore exact card, then make a target-year row with matching digest.
            target_rows = rows[:-1] + [rows[-1] | {"official_date": "2019-01-01"}]
            row_bytes = b"".join(canonical_json_bytes(row) for row in target_rows)
            changed = payload | {
                "calibration_rows_sha256": sha256(row_bytes).hexdigest()
            }
            changed_bytes = canonical_json_bytes(changed)
            paths.rows.write_bytes(row_bytes)
            paths.rows_sidecar.write_bytes(
                canonical_json_bytes({"artifact_sha256": sha256(row_bytes).hexdigest()})
            )
            paths.card_sidecar.write_bytes(
                canonical_json_bytes(
                    {"artifact_sha256": sha256(changed_bytes).hexdigest()}
                )
            )
            paths.card.write_bytes(changed_bytes)
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)

    def test_present_authority_rejects_bound_inputs_and_reread_drift(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            load_probability_calibration_card,
        )

        with self.assertRaisesRegex(ValueError, "inspection-only"):
            load_probability_calibration_card(
                self.spec, "v5.1", "baseline", "O19", None
            )
        return

    def test_v2_typed_chain_recomputes_and_rereads_every_authority_pair(self) -> None:
        """A fully typed synthetic chain proves the V2 loader never trusts rows."""
        from mlb_backtest.model_improvement import probability_calibration as module
        from mlb_backtest.model_improvement.probability_calibration import (
            calibration_paths_v2,
            load_probability_calibration_card_v2,
        )
        from mlb_backtest.model_improvement.source_gate import SourceGateReceipt

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            root = Path(directory)
            declaration = root / "declarations" / "nested" / "run.json"
            declaration.parent.mkdir(parents=True)
            declaration.write_bytes(b"{}")
            raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
            raw["outputs"]["root"] = str(root / "outputs")
            base = build_run_spec(raw)
            digest = "a" * 64
            authority = replace(
                base.source_authority,
                authority_root="authority",
                source_run_spec=PathDigest("source.json", digest),
                acquisition_contract=PathDigest("contract.json", digest),
                terminal_checkpoint=PathDigest("checkpoint.json", digest),
                inventory=PathDigest("inventory.json", digest),
            )
            receipt_path = root / "receipt" / "calibration_feature_receipt.json"
            pins = dict(
                base.probability_calibration_policy.calibration_feature_receipts
            )
            pins["C19"] = AbsolutePathDigest(str(receipt_path), "0" * 64)
            policy = replace(
                base.probability_calibration_policy,
                calibration_feature_receipts=MappingProxyType(pins),
            )
            spec = replace(
                base,
                source_authority=authority,
                probability_calibration_policy=policy,
                verified_activation=VerifiedRunSpecActivation(
                    str(declaration), digest, run_spec_sha256(base)
                ),
            )
            source_root, source_digest = module._source_authority_digest(spec)
            source_root.mkdir()
            (source_root / "inventory.json").write_bytes(b"inventory")
            spec = replace(
                spec,
                source_authority=replace(
                    authority,
                    inventory=PathDigest(
                        "inventory.json",
                        sha256(b"inventory").hexdigest(),
                    ),
                ),
            )
            source_root, source_digest = module._source_authority_digest(spec)
            rows_path = receipt_path.with_name("calibration_feature_rows.jsonl")
            rows_path.parent.mkdir(parents=True)

            def feature(
                pk: int, official: str, start: str, home: float
            ) -> dict[str, object]:
                payload: dict[str, object] = {
                    "schema": "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_ROW_V1",
                    "model_id": "v5.1",
                    "lane_id": "baseline",
                    "fold_id": "O19",
                    "calibration_key": "C19",
                    "game_pk": pk,
                    "official_date": official,
                    "scheduled_start_utc": start,
                    "max_history_date": "2018-03-31" if pk == 1 else "2018-04-30",
                    "target_year": 2019,
                    "history_start_date": "2018-01-01",
                    "history_end_date": "2018-03-31" if pk == 1 else "2018-04-30",
                    "home_team_id": 1,
                    "away_team_id": 2,
                    "component_scores": {
                        "home": {
                            name: home
                            for name in base.baseline.card.component_weights_pp
                        },
                        "away": {
                            name: 0.0
                            for name in base.baseline.card.component_weights_pp
                        },
                    },
                    "component_qualities": {
                        "home": {
                            name: 1.0
                            for name in base.baseline.card.component_weights_pp
                        },
                        "away": {
                            name: 1.0
                            for name in base.baseline.card.component_weights_pp
                        },
                    },
                    "source_feature_file_sha256": digest,
                    "source_feature_content_sha256": digest,
                    "source_authority_sha256": source_digest,
                }
                payload["feature_row_sha256"] = sha256(
                    canonical_json_bytes(payload)
                ).hexdigest()
                return payload

            feature_rows = [
                feature(1, "2018-04-01", "2018-04-01T17:00:00Z", -1.0),
                feature(2, "2018-05-01", "2018-05-01T17:00:00Z", 1.0),
            ]
            rows_raw = b"".join(canonical_json_bytes(row) for row in feature_rows)
            receipt = {
                "schema": "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_RECEIPT_V1",
                "model_id": "v5.1",
                "lane_id": "baseline",
                "fold_id": "O19",
                "calibration_key": "C19",
                "target_year": 2019,
                "training_start": "2018-01-01",
                "training_end": "2018-12-31",
                "source_authority_sha256": source_digest,
                "source_inventory_sha256": sha256(b"inventory").hexdigest(),
                "game_evidence_inventory_relative_path": "game.json",
                "game_evidence_inventory_sha256": digest,
                "feature_contract_sha256": digest,
                "feature_code_revision": "synthetic",
                "feature_rows_path": str(rows_path),
                "feature_rows_sha256": sha256(rows_raw).hexdigest(),
                "feature_population_sha256": sha256(
                    canonical_json_bytes([1, 2])
                ).hexdigest(),
            }
            receipt_raw = canonical_json_bytes(receipt)
            receipt_path.write_bytes(receipt_raw)
            rows_path.write_bytes(rows_raw)
            for path, bytes_ in ((receipt_path, receipt_raw), (rows_path, rows_raw)):
                path.with_suffix(path.suffix + ".sha256").write_bytes(
                    canonical_json_bytes(
                        {"artifact_sha256": sha256(bytes_).hexdigest()}
                    )
                )
            pins["C19"] = AbsolutePathDigest(
                str(receipt_path), sha256(receipt_raw).hexdigest()
            )
            spec = replace(
                spec,
                probability_calibration_policy=replace(
                    policy, calibration_feature_receipts=MappingProxyType(pins)
                ),
            )
            gate = SourceGateReceipt(
                digest,
                digest,
                digest,
                digest,
                sha256(b"inventory").hexdigest(),
                (digest,),
                digest,
                tuple(range(2018, 2025)),
                "SOURCES_READY",
                digest,
                str(root / "gate.json"),
            )
            evidence = SimpleNamespace(
                path=source_root / "game.json",
                sha256=digest,
                payload={
                    "games": [
                        {
                            "game_pk": 1,
                            "official_date": "2018-04-01",
                            "scheduled_start_utc": "2018-04-01T17:00:00Z",
                            "home_team_id": 1,
                            "away_team_id": 2,
                            "winner_team_id": 2,
                            "home_score": 0,
                            "away_score": 1,
                            "played": True,
                            "terminal_status": "Final",
                            "verification_status": "VERIFIED",
                            "source_game_sha256": "1" * 64,
                        },
                        {
                            "game_pk": 2,
                            "official_date": "2018-05-01",
                            "scheduled_start_utc": "2018-05-01T17:00:00Z",
                            "home_team_id": 1,
                            "away_team_id": 2,
                            "winner_team_id": 1,
                            "home_score": 1,
                            "away_score": 0,
                            "played": True,
                            "terminal_status": "Final",
                            "verification_status": "VERIFIED",
                            "source_game_sha256": "2" * 64,
                        },
                    ]
                },
            )
            weights = SimpleNamespace(
                model_id="v5.1",
                path="tests/fixtures/model_improvement/v51_card.json",
                sha256=digest,
                component_weights_pp=base.baseline.card.component_weights_pp,
            )
            inventory = SimpleNamespace(
                game_evidence_inventory=SimpleNamespace(path="game.json", sha256=digest)
            )
            with (
                patch.object(
                    module, "load_bound_source_gate_receipt", return_value=gate
                ),
                patch.object(module, "parse_inventory", return_value=inventory),
                patch.object(
                    module,
                    "load_verified_game_evidence_inventory",
                    return_value=evidence,
                ),
                patch.object(
                    module, "resolve_prediction_weight_authority", return_value=weights
                ),
            ):
                admitted_receipt = module.load_calibration_feature_receipt(spec, "O19")
                expected, _ = module._derive_rows_v2(spec, admitted_receipt, gate)
                wrong_inventory = replace(
                    admitted_receipt,
                    payload=admitted_receipt.payload
                    | {"game_evidence_inventory_relative_path": "other.json"},
                )
                with self.assertRaises(ValueError):
                    module._derive_rows_v2(spec, wrong_inventory, gate)
                wrong_start_row = replace(
                    admitted_receipt.rows[0],
                    payload=admitted_receipt.rows[0].payload
                    | {"scheduled_start_utc": "2018-04-01T18:00:00Z"},
                )
                with self.assertRaises(ValueError):
                    module._derive_rows_v2(
                        spec,
                        replace(
                            admitted_receipt,
                            rows=(wrong_start_row, *admitted_receipt.rows[1:]),
                        ),
                        gate,
                    )
                paths = calibration_paths_v2(spec, ("v5.1", "baseline", "O19"))
                paths.rows.parent.mkdir(parents=True)
                output_rows = b"".join(canonical_json_bytes(row) for row in expected)
                alpha, beta = module.fit_logistic(
                    (row["edge"] / 10.0 for row in expected),
                    (row["home_win"] for row in expected),
                    iterations=100,
                    l2=1e-6,
                    tolerance=1e-10,
                )
                card = {
                    "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V2",
                    "model_id": "v5.1",
                    "lane_id": "baseline",
                    "fold_id": "O19",
                    "calibration_key": "C19",
                    "target_year": 2019,
                    "training_start": "2018-01-01",
                    "training_end": "2018-12-31",
                    "run_spec_sha256": run_spec_sha256(spec),
                    "source_gate_sha256": digest,
                    "prediction_contract_sha256": spec.prediction_contract_sha256,
                    "calibration_feature_receipt_path": str(receipt_path),
                    "calibration_feature_receipt_sha256": sha256(
                        receipt_raw
                    ).hexdigest(),
                    "weight_authority_path": str(REPOSITORY_ROOT / weights.path),
                    "weight_authority_sha256": digest,
                    "calibration_rows_path": str(paths.rows),
                    "calibration_rows_sha256": sha256(output_rows).hexdigest(),
                    "calibration_population_sha256": sha256(
                        canonical_json_bytes([1, 2])
                    ).hexdigest(),
                    "game_evidence_inventory_relative_path": "game.json",
                    "game_evidence_inventory_sha256": digest,
                    "fit_spec": spec.probability_calibration_policy.fit_spec.as_mapping(),
                    "alpha": alpha,
                    "beta": beta,
                }
                card_raw = canonical_json_bytes(card)
                paths.rows.write_bytes(output_rows)
                paths.card.write_bytes(card_raw)
                for path, bytes_ in ((paths.rows, output_rows), (paths.card, card_raw)):
                    path.with_suffix(path.suffix + ".sha256").write_bytes(
                        canonical_json_bytes(
                            {"artifact_sha256": sha256(bytes_).hexdigest()}
                        )
                    )
                loaded = load_probability_calibration_card_v2(spec, gate, "O19")
                self.assertEqual(loaded.alpha, alpha)
                self.assertEqual(loaded.beta, beta)
                original_derive = module._derive_rows_v2
                calls = 0

                def mutate_after_second_authority_pass(*args):
                    nonlocal calls
                    result = original_derive(*args)
                    calls += 1
                    if calls == 2:
                        paths.rows.write_bytes(output_rows + b" ")
                    return result

                with patch.object(
                    module,
                    "_derive_rows_v2",
                    side_effect=mutate_after_second_authority_pass,
                ):
                    with self.assertRaises(ValueError):
                        load_probability_calibration_card_v2(spec, gate, "O19")
                paths.rows.write_bytes(output_rows)
                for field, value in (
                    ("target_year", 2020),
                    ("training_start", "2018-01-02"),
                    ("training_end", "2018-12-30"),
                ):
                    with self.subTest(field=field):
                        mutated = receipt | {field: value}
                        mutated_raw = canonical_json_bytes(mutated)
                        receipt_path.write_bytes(mutated_raw)
                        receipt_path.with_suffix(
                            receipt_path.suffix + ".sha256"
                        ).write_bytes(
                            canonical_json_bytes(
                                {"artifact_sha256": sha256(mutated_raw).hexdigest()}
                            )
                        )
                        mutated_pins = dict(pins)
                        mutated_pins["C19"] = AbsolutePathDigest(
                            str(receipt_path), sha256(mutated_raw).hexdigest()
                        )
                        mutated_spec = replace(
                            spec,
                            probability_calibration_policy=replace(
                                spec.probability_calibration_policy,
                                calibration_feature_receipts=MappingProxyType(
                                    mutated_pins
                                ),
                            ),
                        )
                        with self.assertRaisesRegex(ValueError, "pool"):
                            module.load_calibration_feature_receipt(mutated_spec, "O19")
                receipt_path.write_bytes(receipt_raw)
                receipt_path.with_suffix(receipt_path.suffix + ".sha256").write_bytes(
                    canonical_json_bytes(
                        {"artifact_sha256": sha256(receipt_raw).hexdigest()}
                    )
                )
                bad_receipt = receipt | {
                    "game_evidence_inventory_relative_path": "other.json"
                }
                receipt_path.write_bytes(canonical_json_bytes(bad_receipt))
                with self.assertRaises(ValueError):
                    load_probability_calibration_card_v2(spec, gate, "O19")
