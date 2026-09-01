from __future__ import annotations

import json
import hashlib
import math
from copy import deepcopy
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator

from mlb_backtest.daily_totals_market_overlay import (
    FIXED_GAME_PKS,
    OVERLAY_SCHEMA,
    POLICY,
    POLICY_SCHEMA,
    PriceSupport,
    SNAPSHOT_SCHEMA,
    STATUS_LABELS,
    TARGET_DATE,
    SupportRates,
    american_net_profit,
    compare_sample_support,
    price_support,
    quoted_break_even,
    select_direction,
    validate_american_price,
    validate_line_half_runs,
    validate_draftkings_snapshot,
    validate_overlay_policy,
    validate_overlay_report,
)
from mlb_backtest.live_top3_contracts import canonical_json_bytes


class SecurePersistenceTests(unittest.TestCase):
    def test_public_secure_persistence_interfaces_exist(self) -> None:
        """Catch removal of the admission/build/persistence public boundary."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for name in (
            "admit_draftkings_snapshot",
            "build_overlay",
            "persist_overlay",
            "load_and_validate_model_authority",
            "BuiltOverlay",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(overlay, name))

    @staticmethod
    def _digest(value: object) -> str:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    @classmethod
    def _authority(cls, root: Path) -> tuple[Path, Path]:
        """Create a complete local 15-game authority without model execution."""
        from tests.test_daily_totals import make_valid_model_receipt

        receipt = make_valid_model_receipt()
        receipt["selected_candidate"] = "TEAM_VENUE_30"
        receipt["content_sha256"] = cls._digest({key: item for key, item in receipt.items() if key != "content_sha256"})
        receipt_bytes = canonical_json_bytes(receipt)
        receipt_relative = Path("audit/experimental_daily_totals/model") / f"{receipt['content_sha256']}.json"
        samples = [8] * 764
        source = receipt["history_source"]
        games = [{
            "game_pk": pk, "official_date": TARGET_DATE,
            "scheduled_start_utc": f"2026-09-01T{16 + index // 60:02d}:{index % 60:02d}:00Z",
            "away_team_id": index + 1, "home_team_id": index + 101, "venue_id": index + 201,
            "status": "PROJECTED", "candidate_id": "TEAM_VENUE_30",
            "away_expected_runs": 5.0, "home_expected_runs": 5.0, "expected_total": 10.0,
            "interval_80": [8, 8], "residual_samples": samples,
            "residual_sha256": cls._digest(samples), "residual_count": 764,
            "feature_cutoff": "2026-08-31", "source_digest": source["payload_sha256"],
            "data_quality_flags": ["ADMITTED_SCHEDULE", "MODEL_ELIGIBLE"],
        } for index, pk in enumerate(FIXED_GAME_PKS)]
        report = {
            "schema": "PROVISIONAL_DAILY_TOTALS_V1", "content_sha256": "",
            "status_labels": receipt["status_labels"], "target_date": TARGET_DATE,
            "cutoff_date": "2026-08-31", "model_receipt_relative": receipt_relative.as_posix(),
            "model_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "source_report_relative": receipt["source_report_relative"],
            "source_report_sha256": receipt["source_report_sha256"],
            "history_source": receipt["history_source"], "live_source": receipt["live_source"],
            "selected_candidate": "TEAM_VENUE_30", "development_metrics": receipt["development_metrics"],
            "final_metrics": receipt["final_metrics"], "games": games,
        }
        report["content_sha256"] = cls._digest({key: item for key, item in report.items() if key != "content_sha256"})
        report_bytes = canonical_json_bytes(report)
        report_relative = Path("reports/experimental_daily_totals/2026-09-01/model.json")
        report_sidecar = hashlib.sha256(report_bytes).hexdigest().encode("ascii") + b"\n"
        trace = {
            "schema": "PROVISIONAL_DAILY_TOTALS_TRACE_V1", "content_sha256": "",
            "model_receipt_relative": receipt_relative.as_posix(),
            "model_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "report_relative": report_relative.as_posix(), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "report_sidecar_sha256": hashlib.sha256(report_sidecar).hexdigest(), "markdown_sha256": "a" * 64,
            "source_report_relative": report["source_report_relative"], "source_report_sha256": report["source_report_sha256"],
            "history_source": report["history_source"], "live_source": report["live_source"],
        }
        trace["content_sha256"] = cls._digest({key: item for key, item in trace.items() if key != "content_sha256"})
        trace_relative = Path("reports/experimental_daily_totals/2026-09-01/model.trace.json")
        for relative, contents in (
            (receipt_relative, receipt_bytes), (receipt_relative.with_suffix(".sha256"), hashlib.sha256(receipt_bytes).hexdigest().encode("ascii") + b"\n"),
            (report_relative, report_bytes), (report_relative.with_suffix(".sha256"), report_sidecar),
            (trace_relative, canonical_json_bytes(trace)),
        ):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
        raw_value = {
            "captured_at_utc": "2026-09-01T14:59:00Z",
            "source_url": "https://sportsbook.draftkings.com/markets",
            "games": [{"game_pk": pk, "status": "QUOTED", "draftkings_event_id": f"event-{pk}", "event_label": f"Game {pk}", "line_half_runs": 17, "over_american_odds": 100, "under_american_odds": 100} for pk in FIXED_GAME_PKS],
        }
        raw_bytes = json.dumps(raw_value, indent=1).encode("utf-8")
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        raw_relative = Path("data/experimental_daily_totals_market/raw") / f"{raw_digest}.json"
        (root / raw_relative).parent.mkdir(parents=True, exist_ok=True)
        (root / raw_relative).write_bytes(raw_bytes)
        (root / raw_relative.with_suffix(".sha256")).write_bytes(raw_digest.encode("ascii") + b"\n")
        return trace_relative, raw_relative

    def test_admit_build_and_trace_commit_use_local_bound_evidence(self) -> None:
        """Catch a write sequence that publishes before all model/raw evidence is bound."""
        from mlb_backtest.daily_totals_market_overlay import (
            admit_draftkings_snapshot, build_overlay, persist_overlay,
        )

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            calls: list[str] = []
            snapshot = admit_draftkings_snapshot(
                root, trace, raw,
                comparison_clock=lambda: calls.append("comparison") or datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(calls, ["comparison"])
            self.assertEqual(snapshot.parent.as_posix(), "audit/experimental_daily_totals_market/draftkings")
            built = build_overlay(
                root, trace, snapshot,
                evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
            )
            self.assertEqual([row["game_pk"] for row in built.report["games"]], list(FIXED_GAME_PKS))
            self.assertEqual({row["decision"] for row in built.report["games"]}, {"RESEARCH_PICK"})
            for pk in FIXED_GAME_PKS:
                self.assertIn(f"| {pk} |".encode("ascii"), built.markdown_bytes)
            paths = persist_overlay(root, built)
            self.assertEqual(paths, (built.report_sidecar_relative, built.report_relative, built.markdown_relative, built.trace_relative))
            self.assertTrue((root / paths[-1]).is_file())
            self.assertEqual(persist_overlay(root, built), paths)

    def test_bad_raw_sidecar_rejects_before_any_snapshot_write(self) -> None:
        """Catch acceptance of a capture whose separately bound digest was altered."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            (root / raw.with_suffix(".sha256")).write_bytes(b"0" * 64 + b"\n")
            with self.assertRaises(ValueError):
                admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
            self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())


class SchemaTests(unittest.TestCase):
    def test_contract_schemas_are_draft_2020_12_closed_roots(self) -> None:
        schema_root = Path(__file__).parents[1] / "schemas"
        names = (
            "experimental_daily_totals_draftkings_snapshot.schema.json",
            "experimental_daily_totals_overlay_policy.schema.json",
            "experimental_daily_totals_overlay.schema.json",
        )
        for name in names:
            with self.subTest(name=name):
                schema = json.loads((schema_root / name).read_text())
                Draft202012Validator.check_schema(schema)
                self.assertIs(schema.get("additionalProperties"), False)

    @staticmethod
    def _digest(value: dict[str, object]) -> str:
        unsigned = dict(value)
        unsigned.pop("content_sha256", None)
        return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()

    @classmethod
    def _snapshot_fixture(cls) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": SNAPSHOT_SCHEMA, "content_sha256": "", "target_date": TARGET_DATE,
            "model_trace_relative": "reports/model.trace.json", "model_trace_sha256": "a" * 64,
            "model_report_relative": "reports/model.json", "model_report_sha256": "b" * 64,
            "comparison_bound_at_utc": "2026-09-01T15:00:00Z", "source_name": "DraftKings",
            "book_id": "DRAFTKINGS", "source_url": "https://sportsbook.draftkings.com/markets",
            "capture_method": "EXTERNAL_MANUAL", "captured_at_utc": "2026-09-01T14:59:00Z",
            "raw_capture_relative": "data/raw.json", "raw_capture_sha256": "c" * 64,
            "raw_capture_byte_length": 1,
            "games": [{"game_pk": pk, "status": "NO_QUOTE", "reason": "NOT_LISTED"} for pk in FIXED_GAME_PKS],
        }
        value["content_sha256"] = cls._digest(value)
        return value

    @classmethod
    def _overlay_fixture(cls) -> dict[str, object]:
        rows = [{
            "game_pk": pk, "expected_total": 8.0, "residual_count": 764, "market_status": "QUOTED",
            "line_half_runs": 17, "over_american_odds": 100, "under_american_odds": 100,
            "over_support": 0.5, "under_support": 0.5, "push_support": 0.0,
            "over_conditional_support": 0.5, "under_conditional_support": 0.5,
            "over_support_margin": 0.0, "under_support_margin": 0.0,
            "selected_side": None, "decision": "PASS", "reason_codes": ["AMBIGUOUS_DIRECTION"],
        } for pk in FIXED_GAME_PKS]
        value: dict[str, object] = {
            "schema": OVERLAY_SCHEMA, "content_sha256": "", "status_labels": list(STATUS_LABELS),
            "target_date": TARGET_DATE, "model_report_relative": "reports/model.json", "model_report_sha256": "b" * 64,
            "model_trace_relative": "reports/model.trace.json", "model_trace_sha256": "a" * 64,
            "market_snapshot_relative": "audit/snapshot.json", "market_snapshot_sha256": "c" * 64,
            "policy_schema": POLICY_SCHEMA,
            "policy_sha256": hashlib.sha256(canonical_json_bytes(dict(POLICY))).hexdigest(),
            "evaluated_at_utc": "2026-09-01T15:00:00Z", "games": rows,
        }
        value["content_sha256"] = cls._digest(value)
        return value

    @classmethod
    def _pick_overlay_fixture(cls) -> dict[str, object]:
        value = cls._overlay_fixture()
        row = value["games"][0]
        row.update({
            "expected_total": 9.5, "over_american_odds": 100, "under_american_odds": -100,
            "over_support": 0.55, "under_support": 0.45, "push_support": 0.0,
            "decision": "RESEARCH_PICK", "reason_codes": [],
        })
        cls._set_supports(row, 0.55, 0.45)
        value["content_sha256"] = cls._digest(value)
        return value

    @classmethod
    def _redigest(cls, value: dict[str, object]) -> dict[str, object]:
        value["content_sha256"] = cls._digest(value)
        return value

    @staticmethod
    def _set_supports(row: dict[str, object], over: float, under: float, push: float = 0.0) -> None:
        over_result = price_support(over, under, row["over_american_odds"])
        under_result = price_support(under, over, row["under_american_odds"])
        row.update({
            "over_support": over, "under_support": under, "push_support": push,
            "over_conditional_support": over_result.conditional_support,
            "under_conditional_support": under_result.conditional_support,
            "over_support_margin": over_result.support_margin,
            "under_support_margin": under_result.support_margin,
            "selected_side": select_direction(over_result, under_result)[0],
        })

    def test_valid_instances_pass_their_json_schemas(self) -> None:
        root = Path(__file__).parents[1] / "schemas"
        snapshot = self._snapshot_fixture()
        overlay = self._overlay_fixture()
        Draft202012Validator(json.loads((root / "experimental_daily_totals_draftkings_snapshot.schema.json").read_text())).validate(snapshot)
        Draft202012Validator(json.loads((root / "experimental_daily_totals_overlay_policy.schema.json").read_text())).validate(dict(POLICY))
        Draft202012Validator(json.loads((root / "experimental_daily_totals_overlay.schema.json").read_text())).validate(overlay)

    def test_schema_population_and_order_rejections(self) -> None:
        root = Path(__file__).parents[1] / "schemas"
        snapshot_schema = json.loads((root / "experimental_daily_totals_draftkings_snapshot.schema.json").read_text())
        overlay_schema = json.loads((root / "experimental_daily_totals_overlay.schema.json").read_text())
        for value in (
            {**self._snapshot_fixture(), "games": self._snapshot_fixture()["games"][:-1]},
            {**self._snapshot_fixture(), "games": self._snapshot_fixture()["games"] + [self._snapshot_fixture()["games"][0]]},
            {**self._snapshot_fixture(), "games": [{**self._snapshot_fixture()["games"][0], "game_pk": 1}] + self._snapshot_fixture()["games"][1:]},
        ):
            with self.subTest(kind="snapshot"):
                with self.assertRaises(Exception):
                    Draft202012Validator(snapshot_schema).validate(value)
        overlay = self._overlay_fixture()
        for games in (overlay["games"][:-1], overlay["games"] + [overlay["games"][0]]):
            with self.subTest(kind="overlay_population"):
                with self.assertRaises(Exception):
                    Draft202012Validator(overlay_schema).validate({**overlay, "games": games})
        reordered = deepcopy(overlay)
        reordered["games"] = list(reversed(reordered["games"]))
        with self.assertRaises(Exception):
            Draft202012Validator(overlay_schema).validate(reordered)

    def test_canonical_rfc3339_and_uniform_value_errors(self) -> None:
        snapshot = self._snapshot_fixture()
        for timestamp in ("2026-09-01T14:59:00.0Z", "2026-09-01T14:59:00.120Z", "2026-09-01T14:59:00+00:00", "2026-09-01T14:59:60Z"):
            invalid = deepcopy(snapshot)
            invalid["captured_at_utc"] = timestamp
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)
        for reason in ({}, [], {"bad": 1}, ["NO_QUOTE"]):
            invalid = deepcopy(snapshot)
            invalid["games"][0]["reason"] = reason
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)
        overlay = self._overlay_fixture()
        for side in ({}, [], {"OVER": 1}, ["OVER"]):
            invalid = deepcopy(overlay)
            invalid["games"][0]["selected_side"] = side
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(side=side):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)

    def test_snapshot_rejection_matrix(self) -> None:
        base = self._snapshot_fixture()
        for field, value in (("source_name", "Other"), ("book_id", "OTHER"), ("capture_method", "AUTOMATED"),
                             ("target_date", "2026-08-31"), ("model_trace_sha256", "A" * 64),
                             ("model_report_relative", "/absolute.json"), ("raw_capture_byte_length", True),
                             ("raw_capture_byte_length", 0), ("model_trace_relative", "../trace.json"),
                             ("comparison_bound_at_utc", "2026-09-01T15:00:00+00:00")):
            invalid = deepcopy(base)
            invalid[field] = value
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)
        for url in ("http://sportsbook.draftkings.com", "https://sportsbook.draftkings.com.evil", "https://user@sportsbook.draftkings.com", "https://sportsbook.draftkings.com:443", "https://sportsbook.draftkings.com#frag", "https://SPORTSBOOK.DRAFTKINGS.COM"):
            invalid = deepcopy(base)
            invalid["source_url"] = url
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)
        games = base["games"]
        for replacement in (
            games[:-1],
            games[:-1] + [deepcopy(games[0])],
            [deepcopy({**games[0], "game_pk": 1})] + games[1:],
        ):
            invalid = deepcopy(base)
            invalid["games"] = replacement
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(games=replacement):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)
        quoted = deepcopy(base)
        quoted["games"][0] = {"game_pk": FIXED_GAME_PKS[0], "status": "QUOTED", "draftkings_event_id": "", "event_label": "e", "line_half_runs": 0, "over_american_odds": True, "under_american_odds": -110, "extra": 1}
        quoted["content_sha256"] = self._digest(quoted)
        with self.assertRaises(ValueError):
            validate_draftkings_snapshot(quoted)

    def test_policy_rejection_matrix(self) -> None:
        for field, value in (("schema", "OTHER"), ("required_residual_count", 763), ("maximum_quote_age_seconds", True), ("minimum_nonpush_support", float("nan"))):
            invalid = dict(POLICY)
            invalid[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_overlay_policy(invalid)
        invalid = dict(POLICY)
        invalid["extra"] = 1
        with self.assertRaises(ValueError):
            validate_overlay_policy(invalid)

    def test_overlay_rejection_matrix(self) -> None:
        base = self._overlay_fixture()
        for field, value in (("status_labels", ["bad"]), ("policy_schema", "OTHER"), ("policy_sha256", "d" * 64),
                             ("model_trace_relative", "../trace"), ("market_snapshot_sha256", "A" * 64),
                             ("evaluated_at_utc", "2026-09-01T15:00:00.0Z")):
            invalid = deepcopy(base)
            invalid[field] = value
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)
        for field, value in (("over_support", True), ("under_support", float("inf")), ("push_support", 0.2), ("expected_total", -1), ("residual_count", True)):
            invalid = deepcopy(base)
            invalid["games"][0][field] = value
            if not (isinstance(value, float) and not math.isfinite(value)):
                invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)
        for field, value in (("reason_codes", ["AMBIGUOUS_DIRECTION", "GAME_STARTED"]), ("reason_codes", ["GAME_STARTED", "GAME_STARTED"]), ("reason_codes", [{}]), ("selected_side", []), ("decision", "RESEARCH_PICK")):
            invalid = deepcopy(base)
            invalid["games"][0][field] = value
            invalid["content_sha256"] = self._digest(invalid)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)
        no_quote = deepcopy(base)
        no_quote["games"][0] = {"game_pk": FIXED_GAME_PKS[0], "expected_total": 8.0, "residual_count": 764, "market_status": "NO_QUOTE", "decision": "PASS", "reason_codes": ["NO_QUOTE"], "extra": 1}
        no_quote["content_sha256"] = self._digest(no_quote)
        with self.assertRaises(ValueError):
            validate_overlay_report(no_quote)

    def test_valid_policy_and_snapshot_round_trip_are_defensive(self) -> None:
        policy = validate_overlay_policy(POLICY)
        self.assertEqual(policy, POLICY)
        self.assertIsNot(policy, POLICY)
        snapshot = {
            "schema": SNAPSHOT_SCHEMA, "content_sha256": "",
            "target_date": TARGET_DATE,
            "model_trace_relative": "reports/model.trace.json", "model_trace_sha256": "a" * 64,
            "model_report_relative": "reports/model.json", "model_report_sha256": "b" * 64,
            "comparison_bound_at_utc": "2026-09-01T15:00:00Z",
            "source_name": "DraftKings", "book_id": "DRAFTKINGS",
            "source_url": "https://sportsbook.draftkings.com/markets",
            "capture_method": "EXTERNAL_MANUAL", "captured_at_utc": "2026-09-01T14:59:00Z",
            "raw_capture_relative": "data/raw.json", "raw_capture_sha256": "c" * 64,
            "raw_capture_byte_length": 1,
            "games": [{"game_pk": pk, "status": "NO_QUOTE", "reason": "NOT_LISTED"} for pk in FIXED_GAME_PKS],
        }
        snapshot["content_sha256"] = self._digest(snapshot)
        copied = validate_draftkings_snapshot(snapshot)
        self.assertEqual(copied, snapshot)
        self.assertIsNot(copied, snapshot)
        self.assertIsNot(copied["games"], snapshot["games"])
        snapshot["games"][0]["reason"] = "UNAVAILABLE"
        self.assertEqual(copied["games"][0]["reason"], "NOT_LISTED")

    def test_valid_overlay_round_trip_uses_sealed_game_order(self) -> None:
        rows = [{
            "game_pk": pk, "expected_total": 8.0, "residual_count": 764,
            "market_status": "QUOTED", "line_half_runs": 17,
            "over_american_odds": 100, "under_american_odds": 100,
            "over_support": 0.5, "under_support": 0.5, "push_support": 0.0,
            "over_conditional_support": 0.5, "under_conditional_support": 0.5,
            "over_support_margin": 0.0, "under_support_margin": 0.0,
            "selected_side": None, "decision": "PASS",
            "reason_codes": ["AMBIGUOUS_DIRECTION"],
        } for pk in FIXED_GAME_PKS]
        report = {
            "schema": OVERLAY_SCHEMA, "content_sha256": "", "status_labels": list(STATUS_LABELS),
            "target_date": TARGET_DATE, "model_report_relative": "reports/model.json", "model_report_sha256": "b" * 64,
            "model_trace_relative": "reports/model.trace.json", "model_trace_sha256": "a" * 64,
            "market_snapshot_relative": "audit/snapshot.json", "market_snapshot_sha256": "c" * 64,
            "policy_schema": POLICY_SCHEMA,
            "policy_sha256": hashlib.sha256(canonical_json_bytes(dict(POLICY))).hexdigest(),
            "evaluated_at_utc": "2026-09-01T15:00:00Z", "games": rows,
        }
        report["content_sha256"] = self._digest(report)
        copied = validate_overlay_report(report)
        self.assertEqual(copied, report)
        self.assertIsNot(copied, report)

    def test_fixed_contract_authorities_are_immutable(self) -> None:
        with self.assertRaises(TypeError):
            POLICY["required_residual_count"] = 1  # type: ignore[index]
        with self.assertRaises(TypeError):
            STATUS_LABELS[0] = "ALTERED"  # type: ignore[index]

    def test_direct_validators_reject_stale_hashes_order_and_root_shape(self) -> None:
        snapshot = self._snapshot_fixture()
        snapshot["content_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_draftkings_snapshot(snapshot)
        snapshot = self._snapshot_fixture()
        for invalid in (
            {key: value for key, value in snapshot.items() if key != "games"},
            {**snapshot, "unexpected": True},
        ):
            with self.subTest(snapshot_keys=sorted(invalid)):
                with self.assertRaises(ValueError):
                    validate_draftkings_snapshot(invalid)

        overlay = self._overlay_fixture()
        overlay["content_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_overlay_report(overlay)
        overlay = self._overlay_fixture()
        reordered = deepcopy(overlay)
        reordered["games"] = list(reversed(reordered["games"]))
        reordered["content_sha256"] = self._digest(reordered)
        with self.assertRaises(ValueError):
            validate_overlay_report(reordered)
        for invalid in (
            {key: value for key, value in overlay.items() if key != "games"},
            {**overlay, "unexpected": True},
        ):
            with self.subTest(overlay_keys=sorted(invalid)):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)

    def test_overlay_rejects_forged_derived_claims_and_failed_pick_gates(self) -> None:
        base = self._pick_overlay_fixture()
        for field, value in (("over_conditional_support", 0.56), ("under_conditional_support", 0.44),
                             ("over_support_margin", 0.06), ("under_support_margin", -0.06),
                             ("selected_side", "UNDER")):
            invalid = deepcopy(base)
            invalid["games"][0][field] = value
            self._redigest(invalid)
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)

        cases = []
        invalid = deepcopy(base)
        invalid["games"][0]["residual_count"] = 763
        cases.append(("residual_count", invalid))
        invalid = deepcopy(base)
        self._set_supports(invalid["games"][0], 0.4, 0.3, 0.3)
        cases.append(("nonpush_support", invalid))
        invalid = deepcopy(base)
        invalid["games"][0]["expected_total"] = 9.49
        cases.append(("point_gap", invalid))
        invalid = deepcopy(base)
        self._set_supports(invalid["games"][0], 0.54, 0.46)
        cases.append(("support_margin", invalid))
        for name, invalid in cases:
            invalid["games"][0]["decision"] = "RESEARCH_PICK"
            invalid["games"][0]["reason_codes"] = []
            self._redigest(invalid)
            with self.subTest(gate=name):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)

    def test_overlay_accepts_exact_pick_thresholds_and_rejects_just_below(self) -> None:
        exact = self._pick_overlay_fixture()
        validate_overlay_report(exact)
        for name, expected_total, over, under in (
            ("margin", 9.5, 0.549999, 0.450001),
            ("point_gap", 9.499999, 0.55, 0.45),
        ):
            invalid = deepcopy(exact)
            row = invalid["games"][0]
            row["expected_total"] = expected_total
            self._set_supports(row, over, under)
            row["decision"] = "RESEARCH_PICK"
            row["reason_codes"] = []
            self._redigest(invalid)
            with self.subTest(gate=name):
                with self.assertRaises(ValueError):
                    validate_overlay_report(invalid)


class PureMathTests(unittest.TestCase):
    def test_price_rules_accept_signed_integer_range(self) -> None:
        self.assertEqual(validate_american_price(100), 100)
        self.assertEqual(validate_american_price(-10000), -10000)
        for value in (0, 99, -99, 100.0, True, "-110", 10001, -10001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_american_price(value)

    def test_half_run_ticks_are_positive_integers(self) -> None:
        self.assertEqual(validate_line_half_runs(19), 19)
        for value in (0, -1, 18.5, True, "19"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_line_half_runs(value)

    def test_dataclasses_are_frozen_and_slot_based(self) -> None:
        rates = SupportRates(0.25, 0.5, 0.25)
        support = PriceSupport(0.5, 110 / 210, 0.5 - 110 / 210)
        self.assertFalse(hasattr(rates, "__dict__"))
        self.assertFalse(hasattr(support, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            rates.over = 0.75  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            support.quoted_break_even = 0.5  # type: ignore[misc]

    def test_compare_sample_support_uses_integer_half_run_identity(self) -> None:
        self.assertEqual(
            compare_sample_support((7, 8, 8, 9), 16),
            SupportRates(0.25, 0.25, 0.50),
        )
        self.assertEqual(
            compare_sample_support((7, 8, 8, 9), 17),
            SupportRates(0.25, 0.75, 0.0),
        )

    def test_compare_sample_support_rejects_invalid_container(self) -> None:
        for samples in ((), [7, 8], "78", {7, 8}):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    compare_sample_support(samples, 16)  # type: ignore[arg-type]

    def test_compare_sample_support_rejects_invalid_samples(self) -> None:
        for samples in ((-1,), (1.0,), (True,), (1, False)):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    compare_sample_support(samples, 16)

    def test_compare_sample_support_rejects_invalid_line(self) -> None:
        for line_half_runs in (0, -1, 16.0, True, "16"):
            with self.subTest(line_half_runs=line_half_runs):
                with self.assertRaises(ValueError):
                    compare_sample_support((7,), line_half_runs)  # type: ignore[arg-type]

    def test_american_price_math(self) -> None:
        self.assertEqual(american_net_profit(150), 1.5)
        self.assertEqual(american_net_profit(-200), 0.5)
        self.assertEqual(quoted_break_even(-110), 110 / 210)

    def test_price_support_uses_conditional_support_and_margin(self) -> None:
        result = price_support(0.75, 0.25, -110)
        self.assertEqual(result.conditional_support, 0.75)
        self.assertEqual(result.quoted_break_even, 110 / 210)
        self.assertEqual(result.support_margin, 0.75 - 110 / 210)

    def test_price_support_accepts_finite_real_probabilities(self) -> None:
        result = price_support(Fraction(3, 4), Fraction(1, 4), -110)
        self.assertIsInstance(result.conditional_support, float)
        self.assertEqual(result.conditional_support, 0.75)
        self.assertIsInstance(result.support_margin, float)

    def test_price_support_returns_none_for_all_pushes(self) -> None:
        self.assertEqual(
            price_support(0.0, 0.0, 100),
            PriceSupport(None, 0.5, None),
        )

    def test_price_support_rejects_invalid_probabilities(self) -> None:
        invalid = (True, "0.5", float("nan"), float("inf"), -0.01, 1.01)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    price_support(value, 0.0, 100)  # type: ignore[arg-type]
        for value in invalid:
            with self.subTest(loss_support=value):
                with self.assertRaises(ValueError):
                    price_support(0.0, value, 100)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            price_support(0.6, 0.41, 100)

    def test_price_support_rejects_overflowing_real_probabilities(self) -> None:
        overflowing = Fraction(10**10_000, 1)
        for side, (win_support, loss_support) in (
            ("win", (overflowing, 0.0)),
            ("loss", (0.0, overflowing)),
        ):
            with self.subTest(side=side):
                with self.assertRaises(ValueError):
                    price_support(win_support, loss_support, 100)  # type: ignore[arg-type]

    def test_price_support_rejects_exact_sum_above_one(self) -> None:
        tiny = Fraction(1, 10**400)
        with self.assertRaises(ValueError):
            price_support(Fraction(1, 2), Fraction(1, 2) + tiny, 100)

    def test_price_support_preserves_tiny_positive_nonpush(self) -> None:
        tiny = Fraction(1, 10**400)
        result = price_support(tiny, tiny, 100)
        self.assertEqual(result.conditional_support, 0.5)
        self.assertEqual(result.support_margin, 0.0)

    def test_price_support_rejects_invalid_price(self) -> None:
        with self.assertRaises(ValueError):
            price_support(0.5, 0.5, 99)

    def test_net_profit_and_break_even_reject_invalid_prices(self) -> None:
        for function in (american_net_profit, quoted_break_even):
            with self.subTest(function=function.__name__):
                with self.assertRaises(ValueError):
                    function(99)
                with self.assertRaises(ValueError):
                    function(True)  # type: ignore[arg-type]

    def test_select_direction_chooses_strictly_larger_margin(self) -> None:
        over = price_support(0.8, 0.2, -110)
        under = price_support(0.6, 0.4, -110)
        self.assertEqual(select_direction(over, under), ("OVER", ()))
        self.assertEqual(select_direction(under, over), ("UNDER", ()))

    def test_select_direction_marks_tied_margins_ambiguous(self) -> None:
        over = PriceSupport(0.6, 0.5, 0.1)
        under = PriceSupport(0.7, 0.5, 0.1)
        self.assertEqual(select_direction(over, under), (None, ("AMBIGUOUS_DIRECTION",)))

    def test_select_direction_handles_unavailable_side(self) -> None:
        available = PriceSupport(0.55, 0.5, 0.05)
        unavailable = PriceSupport(None, 0.5, None)
        self.assertEqual(select_direction(available, unavailable), ("OVER", ()))
        self.assertEqual(select_direction(unavailable, available), ("UNDER", ()))
        self.assertEqual(
            select_direction(unavailable, unavailable),
            (None, ("AMBIGUOUS_DIRECTION",)),
        )

    def test_support_rates_are_exact_counts_over_sample_count(self) -> None:
        result = compare_sample_support((0, 1, 2), 2)
        self.assertTrue(math.isclose(result.over, 1 / 3))
        self.assertTrue(math.isclose(result.under, 1 / 3))
        self.assertEqual(result.push, 1 / 3)


if __name__ == "__main__":
    unittest.main()
