"""Research outcomes preserve official type and terminal semantics."""
import unittest
import copy
import tempfile
import json
from dataclasses import replace
from unittest import mock
from pathlib import Path
from datetime import datetime, timezone

from tests.test_postseason_research_daily import build_research_fixture
from tests.test_postseason_research_flow import official_fetcher, durable
from mlb_backtest.prospective_ml_postseason_research import run_research_pregame, read_research_status, load_claim, load_snapshot, load_receipt, read_json, ROOT, sha, seal, write_json
from mlb_backtest.market_play_contracts import canonical_json_bytes
from mlb_backtest.prospective_ml_postseason_settlement import run_postgame, collect_official, persist_settlement, load_head, build_summary


class ResearchSettlementModuleTests(unittest.TestCase):
    def test_final_type_identity_and_tie_are_not_coerced(self):
        from mlb_backtest import prospective_ml_postseason_settlement as module
        snapshot = {"game_pk": 42, "game_type": "W", "target_date": "2026-07-21", "away_team_id": 110, "home_team_id": 120}
        game = {"gamePk": 42, "gameType": "W", "officialDate": "2026-07-21", "status": {"abstractGameState": "Final", "detailedState": "Final"}, "teams": {"away": {"team": {"id": 110}, "score": 3, "isWinner": False}, "home": {"team": {"id": 120}, "score": 5, "isWinner": True}}}
        raw = {"dates": [{"date": "2026-07-21", "games": [game]}]}
        self.assertEqual(module.parse_result(raw, snapshot)["winner"], "home")
        game["gameType"] = "R"
        with self.assertRaises(ValueError):
            module.parse_result(raw, snapshot)
        game["gameType"] = "W"
        game["teams"]["away"].update(score=5, isWinner=False)
        game["teams"]["home"]["isWinner"] = False
        self.assertEqual(module.parse_result(raw, snapshot)["state"], "EXCLUDED")

    def test_supported_types_pending_suspended_no_contest_and_substitutions(self):
        from mlb_backtest.prospective_ml_postseason_settlement import parse_result
        s = {"game_pk": 42, "game_type": "F", "target_date": "2026-07-21", "away_team_id": 110, "home_team_id": 120}
        g = {"gamePk": 42, "gameType": "F", "officialDate": "2026-07-21", "status": {"abstractGameState": "Final", "detailedState": "Final"}, "teams": {"away": {"team": {"id": 110}, "score": 4, "isWinner": True, "probablePitcher": {"id": 999}}, "home": {"team": {"id": 120}, "score": 1, "isWinner": False}}}
        for kind in ("F", "D", "L", "W"):
            s["game_type"] = g["gameType"] = kind
            self.assertEqual(parse_result({"dates": [{"games": [g]}]}, s)["winner"], "away")
        for abstract, detailed, state in (("Live", "In Progress", "PENDING"), ("Live", "Suspended", "PENDING"), ("Final", "No Contest", "EXCLUDED"), ("Final", "Cancelled", "EXCLUDED")):
            g["status"] = {"abstractGameState": abstract, "detailedState": detailed}
            result = parse_result({"dates": [{"games": [g]}]}, s)
            self.assertEqual(result["state"], state)
            self.assertIsNone(result["winner"])
        for key, value in (("gamePk", 43), ("gameType", "P"), ("officialDate", "2026-07-22")):
            invalid = copy.deepcopy(g)
            invalid[key] = value
            with self.assertRaises(ValueError):
                parse_result({"dates": [{"games": [invalid]}]}, s)
        g["teams"]["away"]["team"]["id"] = 999
        with self.assertRaises(ValueError):
            parse_result({"dates": [{"games": [g]}]}, s)


class ResearchSettlementEvidenceTests(unittest.TestCase):
    def fixture(self, root):
        payload, path, _, _ = build_research_fixture(root, count=1)
        pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
        self.assertEqual(len(pre.receipt["primary_claims"]), 1)
        return payload, pre

    def test_pending_and_suspended_advance_to_final_without_duplicate_claim(self):
        for detail in ("In Progress", "Suspended"):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                payload, pre = self.fixture(root)
                pending = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail=detail), durable_clock=durable)
                self.assertEqual(pending.receipt["games"][0]["state"], "PENDING")
                zero = read_json(root, pending.receipt["report_relative"], "summary")
                self.assertEqual(zero["sample_count"], 0)
                self.assertIsNone(zero["accuracy"])
                self.assertIsNone(zero["brier_score"])
                final = run_postgame(root, pre.receipt_relative, previous_postgame_receipt=pending.receipt_relative, fetcher=official_fetcher(payload["games"], completed="2026-07-22T02:00:00Z"), durable_clock=lambda: "2026-07-22T02:00:03Z")
                self.assertEqual(final.receipt["games"][0]["state"], "SETTLED")
                head = load_head(root, final.receipt["settlement_heads"][0])
                self.assertEqual(head["revision"], 2)
                self.assertEqual(head["previous_head_relative"], pending.receipt["settlement_heads"][0])
                self.assertEqual(len(list((root / ROOT / "primary_claims").glob("*.json"))), 1)

    def test_final_tie_and_no_contest_are_excluded_not_pending(self):
        for detail, tie in (("Final", True), ("No Contest", False)):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                payload, pre = self.fixture(root)
                post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail=detail, tie=tie), durable_clock=durable)
                summary = read_json(root, post.receipt["report_relative"], "summary")
                self.assertEqual(summary["sample_count"], 0)
                self.assertEqual(summary["excluded_count"], 1)
                self.assertEqual(summary["pending_count"], 0)
                self.assertIsNone(summary["log_loss"])
                self.assertEqual(post.receipt["games"][0]["state"], "EXCLUDED")

    def test_explicit_correction_requires_newer_changed_result_and_authenticates_prior(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            prior = post.receipt["settlement_heads"][0]
            claim_path = pre.receipt["primary_claims"][0]
            claim = load_claim(root, claim_path)
            snapshot = load_snapshot(root, claim["snapshot_relative"])
            corrected = collect_official(root, snapshot, fetcher=official_fetcher(payload["games"], completed="2026-07-22T02:00:00Z", away_wins=True), durable_clock=lambda: "2026-07-22T02:00:03Z")
            with self.assertRaisesRegex(ValueError, "explicit correction"):
                persist_settlement(root, claim_path, corrected, prior=prior)
            head = persist_settlement(root, claim_path, corrected, prior=prior, correction=True)
            self.assertEqual(load_head(root, head)["result"]["winner"], "away")
            self.assertEqual(load_head(root, head)["revision"], 2)
            self.assertEqual(persist_settlement(root, claim_path, corrected, prior=head, correction=True), head)
            summary = build_summary(root, [claim_path], [head])
            self.assertEqual(summary["sample_count"], 1)
            (root / Path(prior).with_suffix(".json.sha256")).write_bytes(b"corrupt\n")
            with self.assertRaises(ValueError):
                load_head(root, head)

    def test_stale_correction_is_rejected_before_publishing_invalid_head(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            prior = post.receipt["settlement_heads"][0]
            claim_path = pre.receipt["primary_claims"][0]
            claim = load_claim(root, claim_path)
            snapshot = load_snapshot(root, claim["snapshot_relative"])
            old = collect_official(root, snapshot, fetcher=official_fetcher(payload["games"], completed="2026-07-22T00:30:00Z", away_wins=True), durable_clock=durable)
            with self.assertRaises(ValueError):
                persist_settlement(root, claim_path, old, prior=prior, correction=True)
            self.assertFalse((root / Path(prior).with_name("00000002.json")).exists())

    def test_pending_duplicate_and_cross_cohort_summary_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            first = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail="Suspended"), durable_clock=durable)
            same = run_postgame(root, pre.receipt_relative, previous_postgame_receipt=first.receipt_relative, fetcher=official_fetcher(payload["games"], detail="Suspended", completed="2026-07-22T02:00:00Z"), durable_clock=lambda: "2026-07-22T02:00:03Z")
            self.assertEqual(same.receipt["settlement_heads"], first.receipt["settlement_heads"])
            with self.assertRaises(ValueError):
                build_summary(root, ["audit/prospective_ml_observation/primary_claims/880001/a.json"], same.receipt["settlement_heads"])

    def test_mismatched_final_type_creates_partial_diagnostics_not_success(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            final = official_fetcher(payload["games"])
            def invalid(source, url, **kwargs):
                http = final(source, url, **kwargs)
                raw = json.loads(http.body)
                raw["dates"][0]["games"][0]["gameType"] = "P"
                return replace(http, body=json.dumps(raw).encode())
            with self.assertRaisesRegex(ValueError, "type identity"):
                run_postgame(root, pre.receipt_relative, fetcher=invalid, durable_clock=durable)
            values = [json.loads(p.read_bytes()) for p in (root / ROOT / "receipts").glob("*.json")]
            partials = [v for v in values if v["phase"] == "POSTGAME_PARTIAL"]
            self.assertEqual(len(partials), 1)
            self.assertFalse(partials[0]["workflow_complete"])
            self.assertEqual(partials[0]["games"][0]["state"], "FAILED")
            self.assertTrue(partials[0]["errors"])
            self.assertEqual(partials[0]["settlement_heads"], [])
            self.assertIsNotNone(partials[0]["report_relative"])
            summary = read_json(root, partials[0]["report_relative"], "partial summary")
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(summary["sample_count"], 0)

    def test_corrupt_terminal_head_is_rejected_before_resume_fetch(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            head = Path(post.receipt["settlement_heads"][0])
            (root / head.with_suffix(".json.sha256")).unlink()
            def no_fetch(*args, **kwargs):
                raise AssertionError("corrupt head must fail admission before fetch")
            with self.assertRaises(ValueError):
                run_postgame(root, pre.receipt_relative, previous_postgame_receipt=post.receipt_relative, fetcher=no_fetch, durable_clock=durable)

    def test_resealed_valid_prior_head_replacement_cannot_change_correction_ancestry(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            prior = Path(post.receipt["settlement_heads"][0])
            claim_path = pre.receipt["primary_claims"][0]
            snapshot = load_snapshot(root, load_claim(root, claim_path)["snapshot_relative"])
            correction = collect_official(root, snapshot, fetcher=official_fetcher(payload["games"], away_wins=True, completed="2026-07-22T02:00:00Z"), durable_clock=lambda: "2026-07-22T02:00:03Z")
            current = persist_settlement(root, claim_path, correction, prior=prior, correction=True)
            # Replace revision1 with a different independently valid official
            # observation of the same outcome, recomputing every local digest.
            alternative = collect_official(root, snapshot, fetcher=official_fetcher(payload["games"], completed="2026-07-22T01:30:00Z"), durable_clock=lambda: "2026-07-22T01:30:03Z")
            head = read_json(root, prior, "prior head")
            record = read_json(root, head["record_relative"], "prior record")
            record["official_result_receipt_relative"] = alternative.as_posix()
            record["official_result_receipt_sha256"] = sha((root / alternative).read_bytes())
            record = seal(record)
            replacement = Path(head["record_relative"]).with_name(f"00000001-{sha(canonical_json_bytes(record))}.json")
            write_json(root, replacement, record, "attack replacement record")
            head.update(record_relative=replacement.as_posix(), record_sha256=sha(canonical_json_bytes(record)))
            head = seal(head)
            raw = canonical_json_bytes(head)
            (root / prior).write_bytes(raw)
            (root / prior.with_suffix(".json.sha256")).write_bytes(f"{sha(raw)}\n".encode())
            self.assertEqual(read_json(root, prior, "locally resealed prior")["record_relative"], replacement.as_posix())
            with self.assertRaisesRegex(ValueError, "prior.*digest|ancestry"):
                load_head(root, current)
            with self.assertRaises(ValueError):
                load_receipt(root, post.receipt_relative)
            def no_fetch(*args, **kwargs):
                raise AssertionError("resealed terminal replacement must reject before fetch")
            with self.assertRaises(ValueError):
                run_postgame(root, pre.receipt_relative, previous_postgame_receipt=post.receipt_relative, fetcher=no_fetch, durable_clock=durable)

    def test_receipt_pins_terminal_head_bytes_despite_resealed_valid_replacement(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            post = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            head_path = Path(post.receipt["settlement_heads"][0])
            claim = load_claim(root, pre.receipt["primary_claims"][0])
            index = ROOT / "settlement_heads" / claim["claim_identity_sha256"] / "00000001.json"
            index_value = read_json(root, index, "revision index")
            self.assertEqual(index_value.get("head_relative"), head_path.as_posix())
            self.assertEqual(index_value.get("head_sha256"), sha((root / head_path).read_bytes()))
            # Locally valid raw+seal cannot alter the bytes pinned in an existing
            # receipt, even when all replacement metadata is rehashed.
            head = read_json(root, head_path, "terminal head")
            head["previous_head_sha256"] = "a" * 64
            raw = canonical_json_bytes(seal(head))
            (root / head_path).write_bytes(raw)
            (root / head_path.with_suffix(".json.sha256")).write_bytes(f"{sha(raw)}\n".encode())
            with self.assertRaises(ValueError):
                load_receipt(root, post.receipt_relative)
            def no_fetch(*args, **kwargs):
                raise AssertionError("replacement terminal head must fail before fetch")
            with self.assertRaises(ValueError):
                run_postgame(root, pre.receipt_relative, previous_postgame_receipt=post.receipt_relative, fetcher=no_fetch, durable_clock=durable)

    def test_partial_head_publication_cannot_acquire_a_recording_receipt(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            claim = load_claim(root, pre.receipt["primary_claims"][0])
            index = ROOT / "settlement_heads" / claim["claim_identity_sha256"] / "00000001.json"
            def hook(event, relative=""):
                if event == "after_file_fsync" and relative.startswith((ROOT / "settlement_heads").as_posix()) and relative != index.as_posix() and relative.endswith(".json"):
                    raise OSError("injected head publication failure")
            with mock.patch("mlb_backtest.secure_fs._secure_fs_test_hook", side_effect=hook):
                with self.assertRaisesRegex(OSError, "publication failure"):
                    run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            self.assertEqual(json.loads((root / index).read_bytes())["schema"], "PROSPECTIVE_ML_POSTSEASON_RESEARCH_HEAD_INDEX_V1")
            self.assertFalse((root / index.with_suffix(".json.sha256")).exists())
            def no_fetch(*args, **kwargs):
                raise AssertionError("partial publication must require exact recovery before fetching")
            with self.assertRaises(ValueError):
                run_postgame(root, pre.receipt_relative, fetcher=no_fetch, durable_clock=durable)

    def test_failed_resume_preserves_pending_head_in_loadable_partial_receipt(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            pending = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail="Suspended"), durable_clock=durable)
            def fail_fetch(*args, **kwargs):
                raise OSError("offline result fetch failed")
            with self.assertRaisesRegex(OSError, "fetch failed"):
                run_postgame(root, pre.receipt_relative, previous_postgame_receipt=pending.receipt_relative, fetcher=fail_fetch, durable_clock=lambda: "2026-07-22T02:00:03Z")
            paths = [p for p in (root / ROOT / "receipts").glob("*.json") if json.loads(p.read_bytes())["phase"] == "POSTGAME_PARTIAL"]
            self.assertEqual(len(paths), 1)
            partial = load_receipt(root, paths[0].relative_to(root))
            self.assertFalse(partial["workflow_complete"])
            self.assertEqual(set(partial["settlement_heads"]), set(pending.receipt["settlement_heads"]))
            self.assertEqual(partial["games"][0]["settlement_head"], pending.receipt["settlement_heads"][0])
            summary = read_json(root, partial["report_relative"], "partial summary")
            self.assertEqual(summary["pending_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            final = run_postgame(root, pre.receipt_relative, previous_postgame_receipt=paths[0].relative_to(root), fetcher=official_fetcher(payload["games"], completed="2026-07-22T03:00:00Z"), durable_clock=lambda: "2026-07-22T03:00:03Z")
            self.assertEqual(load_head(root, final.receipt["settlement_heads"][0])["revision"], 2)

    def test_pending_resume_failure_tracks_actual_game_and_preserves_all_heads(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=2)
            pre = run_research_pregame(root, path.with_suffix(".trace.json"), clock=lambda: "2026-07-21T18:00:00Z")
            self.assertEqual(len(pre.receipt["primary_claims"]), 2)
            pending = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail="Suspended"), durable_clock=durable)
            pending_fetch = official_fetcher(payload["games"], detail="Suspended", completed="2026-07-22T02:00:00Z")
            failed_pk = payload["games"][1]["game_pk"]
            def fail_second(source, url, **kwargs):
                if int(url.rsplit("gamePks=", 1)[1]) == failed_pk:
                    raise OSError("second pending fetch failed")
                return pending_fetch(source, url, **kwargs)
            with self.assertRaisesRegex(OSError, "second pending fetch failed"):
                run_postgame(root, pre.receipt_relative, previous_postgame_receipt=pending.receipt_relative, fetcher=fail_second, durable_clock=lambda: "2026-07-22T02:00:03Z")
            paths = [p for p in (root / ROOT / "receipts").glob("*.json") if json.loads(p.read_bytes())["phase"] == "POSTGAME_PARTIAL"]
            self.assertEqual(len(paths), 1)
            partial_path = paths[0].relative_to(root)
            partial = load_receipt(root, partial_path)
            self.assertEqual(set(partial["settlement_heads"]), set(pending.receipt["settlement_heads"]))
            self.assertEqual([row["settlement_head"] for row in partial["games"]], [row["settlement_head"] for row in pending.receipt["games"]])
            self.assertEqual([row["state"] for row in partial["games"]], ["PENDING", "PENDING"])
            self.assertEqual([error["game_pk"] for error in partial["errors"]], [failed_pk])
            summary = read_json(root, partial["report_relative"], "pending partial summary")
            self.assertEqual((summary["sample_count"], summary["pending_count"], summary["failure_count"]), (0, 2, 1))
            status = read_research_status(root, pre.receipt_relative, postgame_receipt=partial_path, clock=lambda: "2026-07-22T02:00:04Z")
            self.assertTrue(status["workflow_processed"])
            self.assertFalse(status["workflow_complete"])
            self.assertFalse(status["complete_evidence"])
            final = run_postgame(root, pre.receipt_relative, previous_postgame_receipt=partial_path, fetcher=official_fetcher(payload["games"], completed="2026-07-22T03:00:00Z"), durable_clock=lambda: "2026-07-22T03:00:03Z")
            self.assertEqual([load_head(root, head)["revision"] for head in final.receipt["settlement_heads"]], [2, 2])
            self.assertEqual(read_json(root, final.receipt["report_relative"], "final summary")["sample_count"], 2)

    def test_fractional_utc_result_and_receipt_ordering_use_instants(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, pre = self.fixture(root)
            pending = run_postgame(root, pre.receipt_relative, fetcher=official_fetcher(payload["games"], detail="Suspended", completed="2026-07-22T01:00:00.1Z"), durable_clock=lambda: "2026-07-22T01:00:00.11Z")
            final = run_postgame(root, pre.receipt_relative, previous_postgame_receipt=pending.receipt_relative, fetcher=official_fetcher(payload["games"], completed="2026-07-22T01:00:00.12Z"), durable_clock=lambda: "2026-07-22T01:00:00.13Z")
            self.assertEqual(load_receipt(root, final.receipt_relative)["games"][0]["state"], "SETTLED")
            self.assertEqual(load_head(root, final.receipt["settlement_heads"][0])["revision"], 2)
