"""Hermetic research admission and first-primary evidence tests."""
import unittest
import tempfile
from pathlib import Path
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from mlb_backtest.prospective_ml_postseason_research import run_research_pregame, load_claim, load_snapshot, load_receipt, ROOT, CLAIM_SCHEMA, write_json, sha, digest, seal, persist_receipt
from mlb_backtest.prospective_ml_observation import validate_primary_claim
from mlb_backtest.market_play_contracts import canonical_json_bytes
from tests.test_postseason_research_daily import build_research_fixture
from tests import test_postseason_research_daily as daily_fixture


class ResearchModuleTests(unittest.TestCase):
    def test_future_verified_game_claims_once_with_real_source_authority(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            first = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            self.assertEqual(len(first.receipt["primary_claims"]), 1)
            claim = load_claim(root, first.receipt["primary_claims"][0])
            second = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:01:00Z")
            self.assertEqual(second.receipt["primary_claims"], first.receipt["primary_claims"])
            self.assertEqual(load_claim(root, second.receipt["primary_claims"][0]), claim)
            self.assertEqual(load_snapshot(root, claim["snapshot_relative"])["observed_at_utc"], "2026-07-21T18:00:00Z")

    def test_claim_at_start_is_diagnostic(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            clock = iter(["2026-07-21T18:00:00Z", payload["games"][0]["scheduled_start_utc"]])
            result = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: next(clock))
            self.assertEqual(result.receipt["primary_claims"], [])
            self.assertEqual(result.receipt["games"][0]["reason_codes"], ["GAME_STARTED_AT_CLAIM"])

    def test_observation_equal_start_never_claims(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            result = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: payload["games"][0]["scheduled_start_utc"])
            self.assertEqual(result.receipt["primary_claims"], [])
            self.assertIn("GAME_STARTED", result.receipt["games"][0]["reason_codes"])

    def test_start_during_claim_publication_is_failure_not_success_receipt(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            times = iter(["2026-07-21T18:00:00Z", "2026-07-21T18:00:01Z", payload["games"][0]["scheduled_start_utc"]])
            with self.assertRaisesRegex(ValueError, "strictly pregame"):
                run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: next(times))
            self.assertEqual(list((root / ROOT / "primary_claims").glob("*.json")), [])
            receipts = [json.loads(p.read_bytes()) for p in (root / ROOT / "receipts").glob("*.json")]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["phase"], "PREGAME_PARTIAL")
            self.assertFalse(receipts[0]["workflow_complete"])

    def test_missing_receipt_seal_and_regular_cohort_claim_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            claim_path = pre.receipt["primary_claims"][0]
            with self.assertRaises(ValueError):
                validate_primary_claim(root, claim_path)
            (root / pre.receipt_relative.with_suffix(".json.sha256")).unlink()
            with self.assertRaises(ValueError):
                load_receipt(root, pre.receipt_relative)
            claim = load_claim(root, claim_path)
            # A syntactically valid V1 artifact with identical game/model identity
            # must still be rejected by the research loader and summary.
            snapshot = load_snapshot(root, claim["snapshot_relative"])
            extra = {"competition_scope", "research_policy_version", "history_population", "validation_status", "research_status", "domain_status", "game_type", "away_team_id", "home_team_id", "feature_sha256", "observation_boundary_utc"}
            regular_snapshot = {k: v for k, v in snapshot.items() if k not in extra}
            regular_snapshot["schema"] = "PROSPECTIVE_ML_OBSERVATION_SNAPSHOT_V1"
            regular_snapshot["content_sha256"] = digest(regular_snapshot)
            candidate = Path("audit/prospective_ml_observation/candidates") / str(claim["game_pk"]) / "candidate.json"
            write_json(root, candidate, regular_snapshot, "regular-shaped candidate")
            regular = Path("audit/prospective_ml_observation/primary_claims") / str(claim["game_pk"]) / f"{claim['model_card_sha256']}.json"
            write_json(root, regular, {"schema": "PROSPECTIVE_ML_PRIMARY_CLAIM_V1", "game_pk": claim["game_pk"], "model_card_sha256": claim["model_card_sha256"], "candidate_relative": candidate.as_posix(), "candidate_sha256": sha(canonical_json_bytes(regular_snapshot)), "claimed_at_utc": claim["claimed_at_utc"]}, "regular-shaped claim")
            self.assertEqual(validate_primary_claim(root, regular)["model_card_sha256"], claim["model_card_sha256"])
            with self.assertRaises(ValueError):
                load_claim(root, regular)
            from mlb_backtest.prospective_ml_postseason_settlement import build_summary
            with self.assertRaises(ValueError):
                build_summary(root, [regular], [])

    def test_diagnostic_unscored_and_unsupported_rows_do_not_claim(self):
        for variant in ("partial", "missing_starter", "unsupported", "at_start", "start_during"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                _, path, _, _ = build_research_fixture(root, count=1, variant=variant)
                result = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
                self.assertEqual(result.receipt["primary_claims"], [])
                self.assertEqual(result.receipt["games"][0]["state"], "DIAGNOSTIC")

    def test_source_tamper_between_build_and_claim_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            source = next(b for b in payload["source_bindings"] if b["binding_role"] == "LIVE_PARTICIPANTS")
            calls = 0
            def clock():
                nonlocal calls
                calls += 1
                if calls == 2:
                    (root / source["payload_relative"]).write_bytes(b"{}")
                return "2026-07-21T18:00:00Z"
            with self.assertRaises(ValueError):
                run_research_pregame(root, path.with_suffix(".trace.json"), clock=clock)
            self.assertEqual(list((root / ROOT / "primary_claims").glob("*.json")), [])

    def test_later_genuine_probability_change_preserves_first_primary_forecast(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            first_report, path, _, _ = build_research_fixture(root, count=1)
            first = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            primary = load_claim(root, first.receipt["primary_claims"][0])
            first_snapshot = load_snapshot(root, primary["snapshot_relative"])
            later_report, later_path, _, _ = build_research_fixture(root, count=1, variant="alternate_starter", observation_prefix="postseason-second")
            self.assertNotEqual(first_report["games"][0]["p_home"], later_report["games"][0]["p_home"])
            later = run_research_pregame(root, later_path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:01:00Z")
            self.assertEqual(later.receipt["primary_claims"], first.receipt["primary_claims"])
            self.assertEqual(load_claim(root, later.receipt["primary_claims"][0]), primary)
            self.assertEqual(load_snapshot(root, primary["snapshot_relative"]), first_snapshot)
            self.assertNotEqual(later.receipt["candidates"], first.receipt["candidates"])

    def test_later_authentic_type_change_cannot_rebind_existing_primary(self):
        class RetypedFetcher(daily_fixture.ResearchFetcher):
            def __call__(self, source, url, *, max_bytes):
                http = super().__call__(source, url, max_bytes=max_bytes)
                query = parse_qs(urlsplit(url).query)
                if "/schedule" in urlsplit(url).path and query.get("startDate") == [daily_fixture.TARGET]:
                    raw = json.loads(http.body)
                    for game in raw["dates"][0]["games"]:
                        game["gameType"] = "D"
                    return replace(http, body=json.dumps(raw).encode())
                return http
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            original = load_claim(root, pre.receipt["primary_claims"][0])
            with mock.patch.object(daily_fixture, "ResearchFetcher", RetypedFetcher):
                later, later_path, _, _ = build_research_fixture(root, count=1, observation_prefix="postseason-retyped")
            self.assertEqual(later["games"][0]["game_type"], "D")
            self.assertEqual(original["game_type"], "F")
            with self.assertRaisesRegex(ValueError, "first-primary identity"):
                run_research_pregame(root, later_path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:01:00Z")
            self.assertEqual(load_claim(root, pre.receipt["primary_claims"][0]), original)

    def test_original_receipt_rejects_resealed_later_primary_replacement(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            first = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            claim_path = Path(first.receipt["primary_claims"][0])
            original = load_claim(root, claim_path)
            _, later_path, _, _ = build_research_fixture(root, count=1, variant="alternate_starter", observation_prefix="replacement-second")
            later = run_research_pregame(root, later_path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:01:00Z")
            candidate = later.receipt["candidates"][0]
            self.assertNotEqual(load_snapshot(root, candidate)["p_home"], load_snapshot(root, original["snapshot_relative"])["p_home"])
            replaced = seal({**original, "snapshot_relative": candidate, "snapshot_sha256": sha((root / candidate).read_bytes()), "claimed_at_utc": "2026-07-21T18:01:00Z"})
            raw = canonical_json_bytes(replaced)
            (root / claim_path).write_bytes(raw)
            (root / claim_path.with_suffix(".json.sha256")).write_bytes(f"{sha(raw)}\n".encode())
            with self.assertRaises(ValueError):
                load_receipt(root, first.receipt_relative)

    def test_clock_advancing_at_claim_write_return_cannot_publish_primary(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            now = "2026-07-21T18:00:00Z"
            def hook(event, relative=""):
                nonlocal now
                if event == "before_return_revalidation" and relative.startswith((ROOT / "primary_claims").as_posix()) and relative.endswith(".json") and json.loads((root / relative).read_bytes())["schema"] == CLAIM_SCHEMA:
                    now = payload["games"][0]["scheduled_start_utc"]
            with mock.patch("mlb_backtest.secure_fs._secure_fs_test_hook", side_effect=hook):
                with self.assertRaisesRegex(ValueError, "strictly pregame"):
                    run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: now)
            receipts = [json.loads(p.read_bytes()) for p in (root / ROOT / "receipts").glob("*.json")]
            self.assertFalse(any(v["phase"] == "PREGAME" and v["workflow_complete"] for v in receipts))
            self.assertFalse(any(p.exists() for p in (root / ROOT / "primary_claims").rglob("*.json.sha256")))

    def test_seal_return_boundary_leaves_no_reader_valid_primary_even_if_diagnostics_fail(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            now = "2026-07-21T18:00:00Z"
            def hook(event, relative=""):
                nonlocal now
                if event == "before_return_revalidation" and relative.startswith((ROOT / "primary_claims").as_posix()) and relative.endswith(".json.sha256"):
                    raw_path = root / relative.removesuffix(".sha256")
                    if json.loads(raw_path.read_bytes())["schema"] == CLAIM_SCHEMA:
                        now = payload["games"][0]["scheduled_start_utc"]
                if event == "after_file_fsync" and relative.startswith((ROOT / "receipts").as_posix()):
                    raise OSError("diagnostic write unavailable")
            with mock.patch("mlb_backtest.secure_fs._secure_fs_test_hook", side_effect=hook):
                with self.assertRaisesRegex(OSError, "diagnostic write unavailable"):
                    run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: now)
            records = [p for p in (root / ROOT / "primary_claims").rglob("*.json") if json.loads(p.read_bytes())["schema"] == CLAIM_SCHEMA]
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].with_suffix(".json.sha256").exists())
            self.assertEqual(list((root / ROOT / "primary_claims").glob("*.json")), [])
            with self.assertRaises(ValueError):
                load_claim(root, records[0].relative_to(root))
            self.assertEqual(list((root / ROOT / "receipts").glob("*.json.sha256")), [])

    def test_fractional_utc_admission_compares_instants_not_strings(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=1)
            calls = 0
            def clock():
                nonlocal calls
                calls += 1
                return "2026-07-21T18:00:00.1Z" if calls == 1 else "2026-07-21T18:00:00.12Z"
            pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=clock)
            self.assertEqual(len(pre.receipt["primary_claims"]), 1)
            self.assertEqual(load_claim(root, pre.receipt["primary_claims"][0])["claimed_at_utc"], "2026-07-21T18:00:00.12Z")
            start = payload["games"][0]["scheduled_start_utc"]
            diagnostic = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: start.removesuffix("Z") + ".1Z")
            self.assertIn("GAME_STARTED", diagnostic.receipt["games"][0]["reason_codes"])

    def test_complete_receipt_cannot_drop_authenticated_slate(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            erased = seal({**pre.receipt, "candidates": [], "primary_claims": [], "games": []})
            forged = persist_receipt(root, erased)
            with self.assertRaisesRegex(ValueError, "coverage|slate"):
                load_receipt(root, forged)

    def test_complete_current_candidates_must_project_current_authenticated_trace(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, path, _, _ = build_research_fixture(root, count=1)
            first = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            _, later_path, _, _ = build_research_fixture(root, count=1, variant="alternate_starter", observation_prefix="projection-second")
            later = run_research_pregame(root, later_path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:01:00Z")
            self.assertEqual(load_receipt(root, later.receipt_relative)["primary_claims"], first.receipt["primary_claims"])
            rows = [{**later.receipt["games"][0], "snapshot_relative": first.receipt["candidates"][0]}]
            substituted = seal({**later.receipt, "candidates": first.receipt["candidates"], "games": rows})
            with self.assertRaisesRegex(ValueError, "current candidate trace"):
                load_receipt(root, persist_receipt(root, substituted))
