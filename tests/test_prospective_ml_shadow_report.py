"""Focused contracts for paired temporal-shadow reporting."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.live_top3_contracts import canonical_json_bytes
from mlb_backtest.live_top3_trace import build_trace_payload
from mlb_backtest.prospective_ml_observation import build_ml_observations, persist_ml_observations
from mlb_backtest.prospective_ml_settlement import build_settlement, persist_settlement
from mlb_backtest.prospective_ml_shadow import build_shadow_attachment, load_frozen_shadow_profile, persist_shadow_attachment
from mlb_backtest.prospective_ml_shadow_report import build_shadow_paired_report, paired_metrics, persist_shadow_paired_report, render_shadow_paired_markdown
from mlb_backtest import prospective_ml_shadow_report as shadow_report
from mlb_backtest.scoring import WEIGHTS
from mlb_backtest.scoring_prep import score_raw_game
from tests.prospective_ml_fixtures import sealed_ml_fixture
from tests.test_prospective_ml_settlement import _official_receipt, _rewrite_receipt

_RECEIPT = Path("reports/model_readiness/2026-09-02/temporal_calibration/prepare-corrected/prepare_receipt.json")


def _raw_game() -> dict[str, object]:
    def metric(value: float) -> dict[str, float]:
        return {"value": value, "quality": 1.0, "sample_size": 200}

    def side(code: str, delta: float) -> dict[str, object]:
        return {
            "team_code": code,
            "starter": {"pitcher_id": 100 if delta < 0 else 200, "career_bf": 220, "season_xwoba": metric(.30 + delta * .01), "k_minus_bb": metric(.15 - delta * .01), "fip": metric(3.5 + delta * .1), "last5_xwoba": metric(.31 + delta * .01), "expected_ip": metric(5.5 - delta * .1), "velocity_change": metric(.5 - delta * .1)},
            "offense": {"players": [{"player_id": number + (0 if delta < 0 else 100), "batting_slot": number} for number in range(1, 10)], "platoon_xwoba": metric(.32 - delta * .01), "last14_xwoba": metric(.33 - delta * .01), "bb_minus_k": metric(-.10 + delta * .01), "barrel_rate": metric(.08 - delta * .01), "iso": metric(.16 - delta * .01)},
            "bullpen": {"season_xwoba": metric(.31 + delta * .01), "k_minus_bb": metric(.12 - delta * .01), "last14_xwoba": metric(.32 + delta * .01), "availability": metric(.8 - delta * .01)},
            "environment": metric(50.0), "defense": metric(50.0), "schedule": metric(55.0),
        }

    return {"game_pk": 777001, "official_date": "2026-09-03", "eligible": True, "reasons": [], "lineup_provenance": "TIMESTAMP_VERIFIED_T45", "away": side("AWY", -1.0), "home": side("HOM", 1.0)}


def _baseline_claim(root: Path) -> Path:
    trace = sealed_ml_fixture(root, p_home=.7, completeness=.4)
    profile = load_frozen_shadow_profile(root)
    raw = _raw_game()
    scored = score_raw_game(raw, profile.normalization)
    trace_path = root / trace
    original = json.loads(trace_path.read_text())
    row = original["games"][0]
    row.update({"raw_features": raw, "feature_sha256": hashlib.sha256(canonical_json_bytes(raw)).hexdigest(), "away_scores": scored["away_scores"], "home_scores": scored["home_scores"], "qualities": scored["qualities"], "contributions": {name: WEIGHTS[name] * (scored["home_scores"][name] - scored["away_scores"][name]) for name in WEIGHTS}, "total_edge": scored["edge"], "edge": scored["edge"]})
    rebuilt = build_trace_payload(primary_report_relative=original["primary_report_relative"], primary_report_content_sha256=original["primary_report_content_sha256"], model_card_sha256=original["model_card_sha256"], games=[row])
    raw_trace = canonical_json_bytes(rebuilt)
    trace_path.write_bytes(raw_trace)
    trace_path.with_suffix(trace_path.suffix + ".sha256").write_text(hashlib.sha256(raw_trace).hexdigest() + "\n")
    built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
    return persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:50:00Z").primary_claim_paths[0]


class ShadowReportTests(unittest.TestCase):
    def _attachment(self, root: Path) -> tuple[Path, dict[str, object]]:
        target = root / _RECEIPT
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path.cwd() / _RECEIPT, target)
        sidecar = _RECEIPT.with_suffix(_RECEIPT.suffix + ".sha256")
        target_sidecar = root / sidecar
        target_sidecar.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path.cwd() / sidecar, target_sidecar)
        claim = _baseline_claim(root)
        built = build_shadow_attachment(root, claim, observed_clock="2026-09-03T19:51:00Z")
        persisted = persist_shadow_attachment(root, built, primary_clock=lambda: "2026-09-03T19:52:00Z")
        return persisted.attachment_path, persisted.attachment

    def test_final_report_reuses_one_authenticated_head_for_both_models(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            receipt = _official_receipt(root, home_won=True)
            settled = persist_settlement(root, build_settlement(root, Path(attachment["baseline"]["snapshot_relative"]), receipt))
            report = build_shadow_paired_report(root, [attachment_path], [settled.head_path]).report
            self.assertEqual((report["paired_count"], report["pending_count"], report["excluded_count"]), (1, 0, 0))
            self.assertEqual(report["pairs"][0]["settlement_head_relative"], settled.head_path.as_posix())
            self.assertEqual(report["pairs"][0]["baseline_model_card_sha256"], attachment["baseline"]["model_card_sha256"])
            markdown = render_shadow_paired_markdown(report)
            self.assertIn("mean predicted home-win probability", markdown)
            self.assertIn("observed home-win rate", markdown)
            persisted_report = persist_shadow_paired_report(root, build_shadow_paired_report(root, [attachment_path], [settled.head_path]))
            self.assertTrue((root / persisted_report).is_file())
            self.assertTrue((root / persisted_report.with_suffix(".md")).is_file())

    def test_missing_head_is_pending_and_metrics_are_null(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, _ = self._attachment(root)
            report = build_shadow_paired_report(root, [attachment_path], []).report
            self.assertEqual((report["pending_count"], report["paired_count"]), (1, 0))
            cohort = report["metrics"]["cohorts"][0]
            self.assertEqual(cohort["profile_sha256"], report["pairs"][0]["profile_sha256"])
            self.assertIsNone(cohort["metrics"]["all"]["baseline"]["brier"])

    def test_nonfinal_head_is_excluded_not_loss(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            receipt = _official_receipt(root, abstract_state="Preview", detailed_state="Postponed")
            head = persist_settlement(root, build_settlement(root, Path(attachment["baseline"]["snapshot_relative"]), receipt)).head_path
            report = build_shadow_paired_report(root, [attachment_path], [head]).report
            self.assertEqual((report["excluded_count"], report["paired_count"]), (1, 0))
            self.assertIsNone(report["metrics"]["paired"]["all"]["candidate"]["accuracy"])

    def test_result_observed_at_commit_boundary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            receipt = _official_receipt(root, home_won=True)
            value = json.loads((root / receipt).read_text())
            value["response_completed_at_utc"] = "2026-09-03T19:52:00Z"
            _rewrite_receipt(root, receipt, value)
            head = persist_settlement(root, build_settlement(root, Path(attachment["baseline"]["snapshot_relative"]), receipt)).head_path
            with self.assertRaisesRegex(ValueError, "predates pair commit"):
                build_shadow_paired_report(root, [attachment_path], [head])

    def test_duplicate_attachment_and_conflicting_heads_reject(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            snapshot = Path(attachment["baseline"]["snapshot_relative"])
            first = persist_settlement(root, build_settlement(root, snapshot, _official_receipt(root, home_won=True)))
            with self.assertRaises(ValueError):
                build_shadow_paired_report(root, [attachment_path, attachment_path], [first.head_path])
            second = persist_settlement(root, build_settlement(root, snapshot, _official_receipt(root, home_won=False), prior=first.head_path))
            with self.assertRaises(ValueError):
                build_shadow_paired_report(root, [attachment_path], [first.head_path, second.head_path])

    def test_unrelated_or_tampered_head_rejects(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            snapshot = Path(attachment["baseline"]["snapshot_relative"])
            head = persist_settlement(root, build_settlement(root, snapshot, _official_receipt(root))).head_path
            path = root / head
            path.write_bytes(path.read_bytes().replace(b"00000001", b"00000002"))
            with self.assertRaises(ValueError):
                build_shadow_paired_report(root, [attachment_path], [head])

    def test_persistence_rebind_rejects_mutated_attachment(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            attachment_path, attachment = self._attachment(root)
            snapshot = Path(attachment["baseline"]["snapshot_relative"])
            head = persist_settlement(root, build_settlement(root, snapshot, _official_receipt(root))).head_path
            built = build_shadow_paired_report(root, [attachment_path], [head])
            (root / attachment_path).write_bytes((root / attachment_path).read_bytes() + b"\n")
            with self.assertRaises(ValueError):
                shadow_report.persist_shadow_paired_report(root, built)

    def test_profile_and_completeness_cohorts_remain_separate(self) -> None:
        rows = [
            {"baseline_p_home": .7, "candidate_p_home": .6, "home_won": True, "completeness_stratum": "completeness_ge_0_50"},
            {"baseline_p_home": .4, "candidate_p_home": .6, "home_won": False, "completeness_stratum": "completeness_lt_0_50"},
        ]
        rows[0]["baseline_model_card_sha256"] = "baseline-a"
        rows[0]["profile_sha256"] = "candidate-profile"
        rows[1]["baseline_model_card_sha256"] = "baseline-b"
        rows[1]["profile_sha256"] = "candidate-profile"
        cohorts = shadow_report._cohort_metrics(rows)
        self.assertEqual(len(cohorts), 2)
        self.assertEqual({cohort["baseline_model_card_sha256"] for cohort in cohorts}, {"baseline-a", "baseline-b"})
        self.assertTrue(all(cohort["profile_sha256"] == "candidate-profile" for cohort in cohorts))
        self.assertTrue(all(cohort["metrics"]["all"]["baseline"]["n"] == 1 for cohort in cohorts))

    def test_paired_metrics_hand_example_and_empty_metrics(self) -> None:
        metrics = paired_metrics([{"baseline_p_home": .7, "candidate_p_home": .6, "home_won": True}, {"baseline_p_home": .4, "candidate_p_home": .6, "home_won": False}])
        self.assertEqual((metrics["baseline"]["brier"], metrics["baseline"]["accuracy"], metrics["candidate"]["brier"], metrics["candidate"]["accuracy"]), ("0.125000", "1.000000", "0.260000", "0.500000"))
        self.assertAlmostEqual(float(metrics["baseline"]["log_loss"]), .433750283852, places=12)
        self.assertAlmostEqual(float(metrics["candidate"]["log_loss"]), .713558177820, places=12)
        self.assertIsNone(paired_metrics([])["baseline"]["brier"])

    def test_empty_report_is_valid_without_fabricated_samples(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            built = build_shadow_paired_report(root, [], [])
            self.assertEqual((built.report["primary_count"], built.report["paired_count"]), (0, 0))
            self.assertIsNone(built.report["metrics"]["paired"]["all"]["candidate"]["brier"])
            self.assertTrue((root / persist_shadow_paired_report(root, built)).is_file())


if __name__ == "__main__":
    unittest.main()
