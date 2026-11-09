"""Focused contracts for frozen same-input shadow attachments."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.live_top3_contracts import canonical_json_bytes
from mlb_backtest.live_top3_trace import build_trace_payload
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
)
from mlb_backtest.scoring_prep import score_raw_game
from mlb_backtest.scoring import WEIGHTS
from mlb_backtest.prospective_ml_shadow import (
    build_shadow_attachment,
    load_frozen_shadow_profile,
    load_shadow_attachment,
    main,
    persist_shadow_attachment,
)
from tests.prospective_ml_fixtures import sealed_ml_fixture


_RECEIPT = Path(
    "reports/model_readiness/2026-09-02/temporal_calibration/"
    "prepare-corrected/prepare_receipt.json"
)


def _raw_game() -> dict[str, object]:
    def metric(value: float) -> dict[str, float]:
        return {"value": value, "quality": 1.0, "sample_size": 200}

    def side(code: str, delta: float) -> dict[str, object]:
        return {
            "team_code": code,
            "starter": {
                "pitcher_id": 100 if delta < 0 else 200,
                "career_bf": 220,
                "season_xwoba": metric(.30 + delta * .01),
                "k_minus_bb": metric(.15 - delta * .01),
                "fip": metric(3.5 + delta * .1),
                "last5_xwoba": metric(.31 + delta * .01),
                "expected_ip": metric(5.5 - delta * .1),
                "velocity_change": metric(.5 - delta * .1),
            },
            "offense": {
                "players": [
                    {"player_id": number + (0 if delta < 0 else 100), "batting_slot": number}
                    for number in range(1, 10)
                ],
                "platoon_xwoba": metric(.32 - delta * .01),
                "last14_xwoba": metric(.33 - delta * .01),
                "bb_minus_k": metric(-.10 + delta * .01),
                "barrel_rate": metric(.08 - delta * .01),
                "iso": metric(.16 - delta * .01),
            },
            "bullpen": {
                "season_xwoba": metric(.31 + delta * .01),
                "k_minus_bb": metric(.12 - delta * .01),
                "last14_xwoba": metric(.32 + delta * .01),
                "availability": metric(.8 - delta * .01),
            },
            "environment": metric(50.0),
            "defense": metric(50.0),
            "schedule": metric(55.0),
        }

    return {
        "game_pk": 777001,
        "official_date": "2026-09-03",
        "eligible": True,
        "reasons": [],
        "lineup_provenance": "TIMESTAMP_VERIFIED_T45",
        "away": side("AWY", -1.0),
        "home": side("HOM", 1.0),
    }


class ProspectiveMlShadowTests(unittest.TestCase):
    """A broken source/profile/timing boundary must prevent shadow attachment."""

    def _root_with_profile(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        root = Path(temporary.name)
        source_root = Path.cwd()
        for relative in (_RECEIPT, _RECEIPT.with_name("prepare_receipt.json.sha256")):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, target)
        return temporary, root

    def _baseline_claim(self, root: Path) -> Path:
        trace = sealed_ml_fixture(root, p_home=.7, completeness=.4)
        profile = load_frozen_shadow_profile(root)
        raw = _raw_game()
        scored = score_raw_game(raw, profile.normalization)
        trace_path = root / trace
        original = json.loads(trace_path.read_text())
        row = original["games"][0]
        row.update(
            {
                "raw_features": raw,
                "feature_sha256": hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
                "away_scores": scored["away_scores"],
                "home_scores": scored["home_scores"],
                "qualities": scored["qualities"],
                "contributions": {
                    name: WEIGHTS[name]
                    * (scored["home_scores"][name] - scored["away_scores"][name])
                    for name in WEIGHTS
                },
                "total_edge": scored["edge"],
                "edge": scored["edge"],
            }
        )
        rebuilt = build_trace_payload(
            primary_report_relative=original["primary_report_relative"],
            primary_report_content_sha256=original["primary_report_content_sha256"],
            model_card_sha256=original["model_card_sha256"],
            games=[row],
        )
        trace_path.write_bytes(canonical_json_bytes(rebuilt))
        trace_path.with_suffix(trace_path.suffix + ".sha256").write_text(
            hashlib.sha256(canonical_json_bytes(rebuilt)).hexdigest() + "\n"
        )
        built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
        persisted = persist_ml_observations(
            root, built, primary_clock=lambda: "2026-09-03T19:45:00Z"
        )
        return persisted.primary_claim_paths[0]

    def _trace_path(self, root: Path) -> Path:
        return root / "reports/experimental_daily/2026-09-03/sealed.trace.json"

    def _write_trace(self, root: Path, value: dict[str, object]) -> None:
        path = self._trace_path(root)
        raw = canonical_json_bytes(value)
        path.write_bytes(raw)
        path.with_suffix(path.suffix + ".sha256").write_text(
            hashlib.sha256(raw).hexdigest() + "\n"
        )

    def test_prestart_attachment_binds_same_trace_raw_and_is_idempotent(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        claim = self._baseline_claim(root)

        built = build_shadow_attachment(
            root, claim, observed_clock="2026-09-03T19:46:00Z"
        )
        pair = built.attachment
        self.assertNotEqual(
            float(pair["candidate"]["p_home"]), float(pair["baseline"]["p_home"])
        )
        self.assertEqual(
            pair["feature_sha256"], hashlib.sha256(canonical_json_bytes(_raw_game())).hexdigest()
        )
        self.assertEqual(pair["promotion"], "NO_PROMOTION")
        persisted = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        self.assertEqual(persisted.attachment["commit_claimed_at_utc"], "2026-09-03T19:47:00Z")
        self.assertEqual(
            load_shadow_attachment(root, persisted.attachment_path), persisted.attachment
        )
        replay = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T20:00:00Z"
        )
        self.assertEqual(replay.attachment_path, persisted.attachment_path)

    def test_candidate_observation_before_baseline_evidence_rejects_attachment(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(ValueError):
            build_shadow_attachment(
                root,
                self._baseline_claim(root),
                observed_clock="2026-09-03T19:44:59Z",
            )

    def test_start_boundary_rejects_commit(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        with self.assertRaises(ValueError):
            persist_shadow_attachment(
                root, built, primary_clock=lambda: "2026-09-03T20:00:00Z"
            )

    def test_missing_receipt_sidecar_rejects_profile(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        (root / _RECEIPT.with_name("prepare_receipt.json.sha256")).unlink()
        with self.assertRaises(ValueError):
            load_frozen_shadow_profile(root)

    def test_mutated_trace_rejects_persistence_revalidation(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        self._trace_path(root).write_bytes(b"{}")
        with self.assertRaises(ValueError):
            persist_shadow_attachment(
                root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
            )

    def test_trace_baseline_probability_mismatch_rejects_attachment(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        claim = self._baseline_claim(root)
        original = json.loads(self._trace_path(root).read_text())
        row = original["games"][0]
        row["p_home"] = .6
        row["predicted_side"] = "home"
        row["pick_probability"] = .6
        rebuilt = build_trace_payload(
            primary_report_relative=original["primary_report_relative"],
            primary_report_content_sha256=original["primary_report_content_sha256"],
            model_card_sha256=original["model_card_sha256"],
            games=[row],
        )
        self._write_trace(root, rebuilt)
        with self.assertRaises(ValueError):
            build_shadow_attachment(root, claim, observed_clock="2026-09-03T19:46:00Z")

    def test_nonfinite_raw_trace_value_rejects_attachment(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        claim = self._baseline_claim(root)
        trace = json.loads(self._trace_path(root).read_text())
        trace["games"][0]["raw_features"]["home"]["starter"]["fip"]["value"] = float("nan")
        self._trace_path(root).write_text(json.dumps(trace, sort_keys=True, separators=(",", ":")))
        with self.assertRaises(ValueError):
            build_shadow_attachment(root, claim, observed_clock="2026-09-03T19:46:00Z")

    def test_self_digested_candidate_probability_tampering_rejects_load(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        persisted = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        path = root / persisted.attachment_path
        value = json.loads(path.read_text())
        value["candidate"] = {
            "edge": 99.0,
            "p_home": .2,
            "predicted_side": "away",
            "pick_probability": .8,
        }
        value["content_sha256"] = hashlib.sha256(
            canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"})
        ).hexdigest()
        raw = canonical_json_bytes(value)
        path.write_bytes(raw)
        path.with_suffix(path.suffix + ".sha256").write_text(
            hashlib.sha256(raw).hexdigest() + "\n"
        )
        with self.assertRaises(ValueError):
            load_shadow_attachment(root, persisted.attachment_path)

    def test_commit_before_candidate_staging_time_rejects_load(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        persisted = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )

        marker_path = root / persisted.attachment_path
        marker = json.loads(marker_path.read_text())
        old_candidate_path = root / marker["candidate_relative"]
        candidate = json.loads(old_candidate_path.read_text())
        candidate["staged_at_utc"] = "2026-09-03T19:48:00Z"
        candidate["content_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in candidate.items() if key != "content_sha256"}
            )
        ).hexdigest()
        candidate_raw = canonical_json_bytes(candidate)
        candidate_path = old_candidate_path.with_name(f"{candidate['content_sha256']}.json")
        candidate_path.write_bytes(candidate_raw)
        candidate_path.with_suffix(candidate_path.suffix + ".sha256").write_text(
            hashlib.sha256(candidate_raw).hexdigest() + "\n"
        )
        old_candidate_path.unlink()
        old_candidate_path.with_suffix(old_candidate_path.suffix + ".sha256").unlink()

        marker["candidate_relative"] = candidate_path.relative_to(root).as_posix()
        marker["candidate_sha256"] = hashlib.sha256(candidate_raw).hexdigest()
        marker["content_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {key: item for key, item in marker.items() if key != "content_sha256"}
            )
        ).hexdigest()
        marker_path.write_bytes(canonical_json_bytes(marker))

        with self.assertRaises(ValueError):
            load_shadow_attachment(root, persisted.attachment_path)

    def test_missing_staged_candidate_sidecar_rejects_load(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        persisted = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        commit = json.loads((root / persisted.attachment_path).read_text())
        path = root / commit["candidate_relative"]
        path.with_suffix(path.suffix + ".sha256").unlink()
        with self.assertRaises(ValueError):
            load_shadow_attachment(root, persisted.attachment_path)

    def test_final_source_rebind_rejects_mutation_before_attachment_commit(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        from mlb_backtest import prospective_ml_shadow

        original = prospective_ml_shadow.write_exclusive
        trace_before = self._trace_path(root).read_bytes()

        def mutate_then_write(trusted, relative, contents, label, *, final_rebind=None):
            if label == "shadow candidate":
                self._trace_path(root).write_bytes(b"{}")
            return original(trusted, relative, contents, label, final_rebind=final_rebind)

        with patch.object(prospective_ml_shadow, "write_exclusive", mutate_then_write):
            with self.assertRaises(ValueError):
                persist_shadow_attachment(
                    root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
                )
        self.assertFalse((root / "audit/prospective_ml_shadow/attachments").exists())
        self._trace_path(root).write_bytes(trace_before)
        with self.assertRaises(ValueError):
            persist_shadow_attachment(
                root, built, primary_clock=lambda: "2026-09-03T20:00:00Z"
            )
        fresh = build_shadow_attachment(
            root, built.baseline_primary_claim, observed_clock="2026-09-03T19:46:00Z"
        )
        committed = persist_shadow_attachment(
            root, fresh, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        self.assertEqual(load_shadow_attachment(root, committed.attachment_path), committed.attachment)

    def test_marker_gate_resamples_clock_after_final_source_validation(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        clock = iter(("2026-09-03T19:47:00Z", "2026-09-03T20:00:00Z"))
        with self.assertRaises(ValueError):
            persist_shadow_attachment(root, built, primary_clock=lambda: next(clock))
        self.assertFalse((root / "audit/prospective_ml_shadow/attachments").exists())

    def test_failed_marker_write_does_not_block_fresh_prestart_commit(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        built = build_shadow_attachment(
            root, self._baseline_claim(root), observed_clock="2026-09-03T19:46:00Z"
        )
        from mlb_backtest import prospective_ml_shadow

        original = prospective_ml_shadow.write_exclusive

        def fail_marker(trusted, relative, contents, label, *, final_rebind=None):
            if label == "shadow attachment commit":
                raise OSError("injected marker failure")
            return original(trusted, relative, contents, label, final_rebind=final_rebind)

        with patch.object(prospective_ml_shadow, "write_exclusive", fail_marker):
            with self.assertRaises(OSError):
                persist_shadow_attachment(
                    root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
                )
        fresh = build_shadow_attachment(
            root, built.baseline_primary_claim, observed_clock="2026-09-03T19:46:00Z"
        )
        committed = persist_shadow_attachment(
            root, fresh, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        self.assertEqual(load_shadow_attachment(root, committed.attachment_path), committed.attachment)

    def test_cli_reuses_existing_attachment_after_start_without_new_prediction(self) -> None:
        temporary, root = self._root_with_profile()
        self.addCleanup(temporary.cleanup)
        claim = self._baseline_claim(root)
        built = build_shadow_attachment(
            root, claim, observed_clock="2026-09-03T19:46:00Z"
        )
        persisted = persist_shadow_attachment(
            root, built, primary_clock=lambda: "2026-09-03T19:47:00Z"
        )
        output = io.StringIO()
        with patch("sys.stdout", output), patch(
            "mlb_backtest.prospective_ml_shadow._now", return_value="2026-09-03T20:00:00Z"
        ):
            self.assertEqual(
                main([
                    "snapshot", "--root", str(root), "--baseline-primary-claim", claim.as_posix()
                ]),
                0,
            )
        self.assertEqual(output.getvalue().strip(), persisted.attachment_path.as_posix())


if __name__ == "__main__":
    unittest.main()
