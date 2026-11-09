"""Research loader must authenticate actual filesystem/source lineage."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

from mlb_backtest.live_top3_contracts import canonical_json_bytes
from mlb_backtest.scoring import WEIGHTS, home_probability, overall_completeness, predicted_pick
from mlb_backtest.scoring_prep import score_raw_game
from mlb_backtest.live_model_card import load_frozen_live_card
from tests.test_postseason_research_daily import build_research_fixture


def rehash(value):
    value["content_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in value.items() if k != "content_sha256"})).hexdigest()
    return value


def rewrite_pair(path, report, trace):
    rehash(report)
    stamp = report["executed_at_utc"].replace("-", "").replace(":", "").replace(".", "")
    path = path.with_name(f"{stamp}-{report['content_sha256'][:16]}.json")
    trace["primary_report_relative"] = (Path(trace["primary_report_relative"]).parent / path.name).as_posix()
    trace["primary_report_content_sha256"] = report["content_sha256"]
    rehash(trace)
    for output, value in ((path, report), (path.with_suffix(".trace.json"), trace)):
        raw = canonical_json_bytes(value)
        output.write_bytes(raw)
        output.with_suffix(output.suffix + ".sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    return path


def rescore(report_row, trace_row, card):
    scored = score_raw_game(trace_row["raw_features"], card.normalization)
    probability = home_probability(scored["edge"], card.alpha, card.beta)
    pick = predicted_pick(trace_row["home_team"], trace_row["away_team"], probability)
    digest = hashlib.sha256(canonical_json_bytes(trace_row["raw_features"])).hexdigest()
    trace_row.update(**{key: scored[key] for key in ("away_scores", "home_scores", "away_qualities", "home_qualities", "qualities", "edge")},
                     contributions={key: WEIGHTS[key] * (scored["home_scores"][key] - scored["away_scores"][key]) for key in WEIGHTS},
                     total_edge=scored["edge"], feature_sha256=digest, p_home=probability,
                     predicted_side=pick["predicted_side"], pick_probability=pick["pick_probability"])
    report_row.update(feature_sha256=digest, p_home=probability, **pick,
                      data_completeness=overall_completeness(scored["qualities"]),
                      starter_edge_abs=abs(scored["home_scores"]["starter"] - scored["away_scores"]["starter"]))


class PostseasonContractTests(unittest.TestCase):
    def test_authenticated_mixed_precision_boundary_roundtrips_at_latest_instant(self):
        from mlb_backtest.postseason_research_contracts import load_postseason_research
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, path, _, _ = build_research_fixture(root, count=1, variant="mixed_precision")
            loaded = load_postseason_research(root, path.with_suffix(".trace.json"))
            self.assertEqual(report["games"][0]["observation_boundary_utc"], "2026-07-21T17:00:02.12Z")
            self.assertEqual(loaded.report["games"][0]["observation_boundary_utc"], "2026-07-21T17:00:02.12Z")

    def test_boundary_maximum_and_prestart_gates_compare_instants_not_text(self):
        from mlb_backtest.postseason_research_contracts import validate_postseason_report, validate_postseason_trace
        with tempfile.TemporaryDirectory() as temporary:
            report, path, _, _ = build_research_fixture(Path(temporary), count=1)
            trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            for boundary, allowed in (("2026-07-21T17:00:02.12Z", True), ("2026-07-21T17:00:02.1Z", False), ("2026-07-21T23:05:00.1Z", False)):
                with self.subTest(boundary=boundary):
                    candidate = deepcopy(report)
                    changed = {}
                    for index, binding in enumerate(candidate["source_bindings"]):
                        old_digest = binding["binding_sha256"]
                        binding["durable_at_utc"] = "2026-07-21T23:05:00.1Z" if "23:05" in boundary else (
                            "2026-07-21T17:00:02.12Z" if index % 2 == 0 else "2026-07-21T17:00:02.1Z")
                        binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in binding.items() if k != "binding_sha256"})).hexdigest()
                        changed[old_digest] = binding["binding_sha256"]
                    row = candidate["games"][0]
                    row["source_binding_sha256s"] = sorted(changed[digest] for digest in row["source_binding_sha256s"])
                    row["observation_boundary_utc"] = boundary
                    row["scheduled_start_utc"] = "2026-07-21T23:05:00Z"
                    if allowed:
                        self.assertEqual(validate_postseason_report(rehash(candidate))["games"][0]["observation_boundary_utc"], boundary)
                    else:
                        with self.assertRaises(ValueError):
                            validate_postseason_report(rehash(candidate))
            trace["games"][0]["scheduled_start_utc"] = "2026-07-21T23:05:00Z"
            trace["games"][0]["observation_boundary_utc"] = "2026-07-21T23:05:00.1Z"
            with self.assertRaises(ValueError):
                validate_postseason_trace(rehash(trace))

    def test_trusted_loader_is_available(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("mlb_backtest.postseason_research_contracts"), "research trusted loader is missing")

    def test_real_scored_roundtrip_and_detached_immutable_authority(self):
        from mlb_backtest.postseason_research_contracts import load_postseason_research
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, path, _, _ = build_research_fixture(root)
            loaded = load_postseason_research(root, path.with_suffix(".trace.json"))
            self.assertEqual(loaded.report["content_sha256"], payload["content_sha256"])
            self.assertEqual(len(loaded.trace["games"]), 4)
            with self.assertRaises(TypeError):
                loaded.report["games"][0]["p_home"] = 0.99

    def test_schema_and_trace_tampering_rejected(self):
        from mlb_backtest.postseason_research_contracts import validate_postseason_report, validate_postseason_trace
        with tempfile.TemporaryDirectory() as temporary:
            payload, path, _, _ = build_research_fixture(Path(temporary))
            forged = dict(payload, rank=1)
            with self.assertRaises(ValueError):
                validate_postseason_report(rehash(forged))
            trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            trace["games"][0]["home_scores"]["starter"] += 1
            with self.assertRaises(ValueError):
                validate_postseason_trace(rehash(trace))

    def test_rehashed_feature_drift_cannot_become_source_authority(self):
        from mlb_backtest.postseason_research_contracts import load_postseason_research
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, path, _, _ = build_research_fixture(root, count=1)
            trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            trace["games"][0]["raw_features"]["home"]["schedule"]["value"] = 20.0
            rescore(report["games"][0], trace["games"][0], load_frozen_live_card(root / "audit/v5_1_bt/model_card.json"))
            path = rewrite_pair(path, report, trace)
            with self.assertRaisesRegex(ValueError, "source-to-feature"):
                load_postseason_research(root, path.with_suffix(".trace.json"))

    def test_card_source_digest_and_population_tampering_fail(self):
        from mlb_backtest.postseason_research_contracts import load_postseason_research
        for variant in ("card", "payload", "trace_missing", "trace_duplicate", "team", "type", "policy", "population"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report, path, _, _ = build_research_fixture(root, count=1)
                trace = json.loads(path.with_suffix(".trace.json").read_bytes())
                if variant == "card":
                    (root / "audit/v5_1_bt/model_card.json").write_bytes(b"{}")
                elif variant == "payload":
                    (root / report["source_bindings"][0]["payload_relative"]).write_bytes(b"{}")
                elif variant == "trace_missing":
                    trace["games"] = []
                elif variant == "trace_duplicate":
                    trace["games"].append(deepcopy(trace["games"][0]))
                elif variant in ("team", "type"):
                    field, value = ("away_team_id", 999) if variant == "team" else ("game_type", "W")
                    report["games"][0][field] = trace["games"][0][field] = value
                elif variant == "policy":
                    report["competition_scope"] = trace["competition_scope"] = "regular-season"
                else:
                    report["history_population"]["recent_game_types"] = ["R"]
                    trace["history_population"]["recent_game_types"] = ["R"]
                path = rewrite_pair(path, report, trace)
                with self.assertRaises(ValueError):
                    load_postseason_research(root, path.with_suffix(".trace.json"))

    def test_scope_stripped_rehashed_legacy_input_rejected_by_raw_source_policy(self):
        from mlb_backtest.live_top3_contracts import RANKED_ROW_KEYS, validate_run_payload
        from mlb_backtest.live_top3_trace import _ROW_KEYS, validate_trace_payload
        from mlb_backtest.market_play_authorities import load_ml_authority
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, path, _, _ = build_research_fixture(root, count=1)
            research_trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            changed = {}
            for binding in report["source_bindings"]:
                old = binding["binding_sha256"]
                identity = json.loads(binding["query_identity_json"])
                for key in ("competition_scope", "research_policy_version", "history_population"):
                    identity.pop(key)
                binding["query_identity_json"] = canonical_json_bytes(identity).decode().removesuffix("\n")
                binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in binding.items() if k != "binding_sha256"})).hexdigest()
                changed[old] = binding["binding_sha256"]
            source_row = report["games"][0]
            ranked = {key: source_row[key] for key in RANKED_ROW_KEYS if key not in {"rank", "pick_probability_display"}}
            ranked.update(rank=1, state="PRELIMINARY", pick_probability_display=f"{ranked['pick_probability']:.1%}",
                          source_binding_sha256s=sorted(changed[d] for d in source_row["source_binding_sha256s"]))
            legacy = {key: report[key] for key in ("model_label", "target_date", "executed_at_eastern", "executed_at_utc", "history_cutoff_date", "model_card_sha256", "source_bindings", "warnings")}
            legacy.update(schema_version=1, command_inputs={"date": report["target_date"], "model_card": report["command_inputs"]["model_card"]},
                          cache_authority={"namespace": "experimental_daily/history_cache", "cutoff_date": report["history_cutoff_date"], "catalog_sha256": report["cache_authority"]["catalog_sha256"]},
                          verified_top3=[], preliminary_top3=[ranked], unscored_games=[], excluded_games=[])
            validate_run_payload(rehash(legacy))  # Deliberately passes exact V1 schema.
            legacy_row = {key: research_trace["games"][0][key] for key in _ROW_KEYS if key not in {"primary_rank", "disposition"}}
            legacy_row.update(primary_rank=1, disposition="PRELIMINARY")
            legacy_trace = {"schema_version": 1, "research_status": "PRELIMINARY_RESEARCH_ONLY", "not_a_betting_recommendation": True,
                            "primary_report_relative": path.relative_to(root).as_posix(), "primary_report_content_sha256": legacy["content_sha256"],
                            "model_card_sha256": legacy["model_card_sha256"], "games": [legacy_row]}
            validate_trace_payload(rehash(legacy_trace))
            path.write_bytes(canonical_json_bytes(legacy))
            path.with_suffix(".trace.json").write_bytes(canonical_json_bytes(legacy_trace))
            for output in (path, path.with_suffix(".trace.json")):
                output.with_suffix(output.suffix + ".sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "\n")
            with self.assertRaisesRegex(ValueError, "policy"):
                load_ml_authority(root, path.with_suffix(".trace.json"))
            # The prior valid legacy receipt contract permits an absent query.
            # Removing it and rehashing every caller-visible reference must not
            # hide policy retained by the authenticated raw source manifest.
            changed_receipts = {}
            for binding in legacy["source_bindings"]:
                if binding["binding_role"] not in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS"}:
                    continue
                receipt_path = root / binding["receipt_relative"]
                receipt = json.loads(receipt_path.read_bytes())
                receipt.pop("query_identity_json")
                raw = canonical_json_bytes(receipt)
                receipt_path.write_bytes(raw)
                old_digest = binding["binding_sha256"]
                binding["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
                binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in binding.items() if k != "binding_sha256"})).hexdigest()
                changed_receipts[old_digest] = binding["binding_sha256"]
            ranked["source_binding_sha256s"] = sorted(changed_receipts.get(digest, digest) for digest in ranked["source_binding_sha256s"])
            path = rewrite_pair(path, legacy, legacy_trace)
            validate_run_payload(legacy)
            validate_trace_payload(legacy_trace)
            with self.assertRaisesRegex(ValueError, "raw manifest policy"):
                load_ml_authority(root, path.with_suffix(".trace.json"))

    def test_unknown_history_query_fields_and_wrong_request_population_fail(self):
        from mlb_backtest.postseason_research_contracts import validate_postseason_report
        for variant in ("extra_query", "wrong_population"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                report, _, _, _ = build_research_fixture(Path(temporary), count=1)
                binding = next(b for b in report["source_bindings"] if b["binding_role"] == "CAREER_BF")
                old_digest = binding["binding_sha256"]
                identity = json.loads(binding["query_identity_json"])
                if variant == "extra_query":
                    identity["unapproved"] = True
                else:
                    binding["requested_url"] = binding["requested_url"].replace("gameType=R", "gameType=F")
                    binding["final_url"] = binding["requested_url"]
                    identity["url"] = binding["requested_url"]
                binding["query_identity_json"] = canonical_json_bytes(identity).decode().removesuffix("\n")
                binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in binding.items() if k != "binding_sha256"})).hexdigest()
                for row in report["games"]:
                    row["source_binding_sha256s"] = sorted(binding["binding_sha256"] if digest == old_digest else digest for digest in row["source_binding_sha256s"])
                with self.assertRaises(ValueError):
                    validate_postseason_report(rehash(report))

    def test_rehashed_omitted_consumed_sources_cannot_move_observation_boundary(self):
        from mlb_backtest.postseason_research_contracts import load_postseason_research
        for omitted in ("LIVE_SCHEDULE", "HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_ERA", "HISTORY_VENUE", "CAREER_BF", "ALL_SHARED"):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report, path, _, _ = build_research_fixture(root, count=1)
                trace = json.loads(path.with_suffix(".trace.json").read_bytes())
                bindings = {binding["binding_sha256"]: binding for binding in report["source_bindings"]}
                refs = [digest for digest in report["games"][0]["source_binding_sha256s"]
                        if bindings[digest]["binding_role"] != omitted and
                        not (omitted == "ALL_SHARED" and bindings[digest]["binding_role"] not in {"LIVE_PARTICIPANTS", "FINAL_SCHEDULE"})]
                boundary = max(bindings[digest]["durable_at_utc"] for digest in refs)
                for row in (report["games"][0], trace["games"][0]):
                    row["source_binding_sha256s"] = refs
                    row["observation_boundary_utc"] = boundary
                path = rewrite_pair(path, report, trace)
                with self.assertRaises(ValueError):
                    load_postseason_research(root, path.with_suffix(".trace.json"))

    def test_legacy_minimal_receipt_remains_valid_authority(self):
        from tests.prospective_ml_fixtures import sealed_ml_fixture
        from mlb_backtest.market_play_authorities import load_ml_authority
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            authority = load_ml_authority(root, trace)
            self.assertEqual(len(authority.games), 1)
            self.assertTrue(authority.games[0].participant.complete)
