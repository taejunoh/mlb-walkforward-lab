from __future__ import annotations

import json
import hashlib
import math
import os
from copy import deepcopy
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    @staticmethod
    def _raw_value(root: Path, raw: Path) -> dict[str, object]:
        return json.loads((root / raw).read_bytes())

    @staticmethod
    def _model_authority_bytes(root: Path, trace: Path) -> list[bytes]:
        trace_path = root / trace
        trace_value = json.loads(trace_path.read_bytes())
        report_path = root / Path(trace_value["report_relative"])
        receipt_path = root / Path(trace_value["model_receipt_relative"])
        paths = (
            trace_path,
            report_path,
            report_path.with_suffix(".sha256"),
            receipt_path,
            receipt_path.with_suffix(".sha256"),
        )
        return [path.read_bytes() for path in paths]

    @staticmethod
    def _write_raw(root: Path, value: object) -> Path:
        contents = json.dumps(value, indent=1).encode("utf-8")
        digest = hashlib.sha256(contents).hexdigest()
        relative = Path("data/experimental_daily_totals_market/raw") / f"{digest}.json"
        (root / relative).write_bytes(contents)
        (root / relative.with_suffix(".sha256")).write_bytes(digest.encode("ascii") + b"\n")
        return relative

    @classmethod
    def _rewrite_model_report(cls, root: Path, trace: Path, mutate: object) -> None:
        """Rewrite and rebind canonical model bytes for a policy fixture."""
        trace_path = root / trace
        trace_value = json.loads(trace_path.read_bytes())
        report_path = root / Path(trace_value["report_relative"])
        report = json.loads(report_path.read_bytes())
        mutate(report)
        report["content_sha256"] = cls._digest({key: item for key, item in report.items() if key != "content_sha256"})
        report_bytes = canonical_json_bytes(report)
        report_sidecar = hashlib.sha256(report_bytes).hexdigest().encode("ascii") + b"\n"
        report_path.write_bytes(report_bytes)
        report_path.with_suffix(".sha256").write_bytes(report_sidecar)
        trace_value["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
        trace_value["report_sidecar_sha256"] = hashlib.sha256(report_sidecar).hexdigest()
        trace_value["content_sha256"] = cls._digest({key: item for key, item in trace_value.items() if key != "content_sha256"})
        trace_path.write_bytes(canonical_json_bytes(trace_value))

    @classmethod
    def _alternate_model_report(cls, root: Path, trace: Path) -> tuple[Path, bytes, str]:
        """Write a second individually valid report with exact content/sidecar bindings."""
        trace_value = json.loads((root / trace).read_bytes())
        report_path = root / Path(trace_value["report_relative"])
        report = json.loads(report_path.read_bytes())
        game = report["games"][0]
        game.update({"away_expected_runs": 6.0, "home_expected_runs": 5.0, "expected_total": 11.0})
        report["content_sha256"] = cls._digest({key: item for key, item in report.items() if key != "content_sha256"})
        contents = canonical_json_bytes(report)
        relative = report_path.with_name("alternate-model.json").relative_to(root)
        sidecar = hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n"
        destination = root / relative
        destination.write_bytes(contents)
        destination.with_suffix(".sha256").write_bytes(sidecar)
        return relative, contents, hashlib.sha256(sidecar).hexdigest()

    @classmethod
    def _alternate_model_receipt(cls, root: Path, trace: Path) -> tuple[Path, bytes, str]:
        """Write a second individually valid receipt with exact content/sidecar bindings."""
        trace_value = json.loads((root / trace).read_bytes())
        receipt_path = root / Path(trace_value["model_receipt_relative"])
        receipt = json.loads(receipt_path.read_bytes())
        receipt["final_metrics"] = dict(receipt["final_metrics"], mae=1.0)
        receipt["content_sha256"] = cls._digest({key: item for key, item in receipt.items() if key != "content_sha256"})
        contents = canonical_json_bytes(receipt)
        relative = Path("audit/experimental_daily_totals/model") / f"{receipt['content_sha256']}.json"
        sidecar = hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n"
        destination = root / relative
        destination.write_bytes(contents)
        destination.with_suffix(".sha256").write_bytes(sidecar)
        return relative, contents, hashlib.sha256(sidecar).hexdigest()

    @classmethod
    def _rewrite_snapshot(cls, root: Path, snapshot: Path, mutate: object) -> Path:
        """Recanonicalize a snapshot and its exact sidecar after one valid-shape mutation."""
        primary = root / snapshot
        value = json.loads(primary.read_bytes())
        mutate(value)
        value["content_sha256"] = cls._digest({key: item for key, item in value.items() if key != "content_sha256"})
        contents = canonical_json_bytes(value)
        relative = snapshot.parent / f"{value['content_sha256']}.json"
        primary.unlink()
        primary.with_suffix(".sha256").unlink()
        destination = root / relative
        destination.write_bytes(contents)
        destination.with_suffix(".sha256").write_bytes(hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n")
        return relative

    @classmethod
    def _build_public_case(
        cls, root: Path, raw_mutator: object | None = None, mutate_report: object | None = None,
    ):
        """Create a fresh authority, admit one capture, and build one overlay."""
        from mlb_backtest.daily_totals_market_overlay import (
            admit_draftkings_snapshot, build_overlay,
        )

        trace, raw = cls._authority(root)
        raw_value = cls._raw_value(root, raw)
        if raw_mutator is not None:
            raw_mutator(raw_value)
        if mutate_report is not None:
            cls._rewrite_model_report(root, trace, mutate_report)
        candidate = cls._write_raw(root, raw_value)
        snapshot = admit_draftkings_snapshot(
            root, trace, candidate,
            comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
        )
        built = build_overlay(
            root, trace, snapshot,
            evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
        )
        return trace, snapshot, built

    @classmethod
    def _admitted(cls, root: Path) -> tuple[Path, Path, Path]:
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        trace, raw = cls._authority(root)
        snapshot = admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
        return trace, raw, snapshot

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

    def test_persist_rejects_forged_bundle_without_rebound_evidence(self) -> None:
        """Catch a caller-created bundle that omits the immutable input chain."""
        from mlb_backtest.daily_totals_market_overlay import (
            admit_draftkings_snapshot, build_overlay, persist_overlay,
        )

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
            built = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            authority_before = self._model_authority_bytes(root, trace)
            forged = replace(built, input_snapshots=())
            with self.assertRaises(ValueError):
                persist_overlay(root, forged)
            self.assertFalse((root / built.trace_relative).exists())
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_persist_rejects_tampered_bytes_and_noncanonical_paths_before_write(self) -> None:
        """Catch a self-consistent caller trace that changes a non-authority field."""
        from mlb_backtest.daily_totals_market_overlay import (
            admit_draftkings_snapshot, build_overlay, persist_overlay,
        )

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
            built = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))

            altered_markdown = b"altered markdown\n"
            altered_trace = json.loads(built.trace_bytes)
            altered_trace["markdown_sha256"] = hashlib.sha256(altered_markdown).hexdigest()
            altered_trace["content_sha256"] = self._digest({key: item for key, item in altered_trace.items() if key != "content_sha256"})
            basename = built.report_relative.with_name("alternate-" + built.report_relative.name)
            basename_trace = json.loads(built.trace_bytes)
            basename_trace["report_relative"] = basename.as_posix()
            basename_trace["content_sha256"] = self._digest({key: item for key, item in basename_trace.items() if key != "content_sha256"})
            cases = (
                replace(built, markdown_bytes=altered_markdown, trace_bytes=canonical_json_bytes(altered_trace)),
                replace(built, trace_bytes=b" " + built.trace_bytes),
                replace(built, report_relative=basename, report_sidecar_relative=basename.with_suffix(".sha256"), markdown_relative=basename.with_suffix(".md"), trace_relative=basename.with_suffix(".trace.json"), trace_bytes=canonical_json_bytes(basename_trace)),
            )
            authority_before = self._model_authority_bytes(root, trace)
            for forged in cases:
                with self.subTest(kind=forged.report_relative.name):
                    with self.assertRaises(ValueError):
                        persist_overlay(root, forged)
                    self.assertFalse((root / forged.trace_relative).exists())
                    self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_persist_rebinds_again_after_parsing_trace(self) -> None:
        """Catch a post-parse replacement that the trace parse itself cannot observe."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = overlay.admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            authority_before = self._model_authority_bytes(root, trace)
            original = overlay.rebind_regular_snapshot
            changed = False

            def replace_after_parse(trusted: object, evidence: object, label: str, **kwargs: object) -> None:
                nonlocal changed
                original(trusted, evidence, label, **kwargs)  # type: ignore[arg-type]
                if not changed and label.startswith("overlay post-trace"):
                    (root / raw).write_bytes(b"changed after trace parse")
                    changed = True

            with patch.object(overlay, "rebind_regular_snapshot", side_effect=replace_after_parse):
                with self.assertRaises(ValueError):
                    overlay.persist_overlay(root, built)
            self.assertTrue(changed)
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_model_authority_rejects_each_missing_or_mutated_component_without_running_model(self) -> None:
        """Catch any authority leaf accepted without its no-follow bound bytes."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for kind in ("trace", "report", "report_sidecar", "receipt", "receipt_sidecar"):
            with self.subTest(kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _ = self._authority(root)
                receipt = next((root / "audit/experimental_daily_totals/model").glob("*.json"))
                paths = {
                    "trace": root / trace,
                    "report": root / "reports/experimental_daily_totals/2026-09-01/model.json",
                    "report_sidecar": root / "reports/experimental_daily_totals/2026-09-01/model.sha256",
                    "receipt": receipt,
                    "receipt_sidecar": receipt.with_suffix(".sha256"),
                }
                paths[kind].write_bytes(b"changed")
                with patch("mlb_backtest.daily_totals.build_daily_totals", side_effect=AssertionError("model rerun")):
                    with self.assertRaises(ValueError):
                        overlay.load_and_validate_model_authority(root, trace)
                self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())

    def test_model_authority_and_raw_admission_reject_each_deleted_leaf_before_clock(self) -> None:
        """Every sealed authority/raw primary and sidecar is required before admission timing."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for kind in ("trace", "report", "report_sidecar", "receipt", "receipt_sidecar", "raw", "raw_sidecar"):
            with self.subTest(kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, raw = self._authority(root)
                receipt = next((root / "audit/experimental_daily_totals/model").glob("*.json"))
                paths = {
                    "trace": root / trace,
                    "report": root / "reports/experimental_daily_totals/2026-09-01/model.json",
                    "report_sidecar": root / "reports/experimental_daily_totals/2026-09-01/model.sha256",
                    "receipt": receipt,
                    "receipt_sidecar": receipt.with_suffix(".sha256"),
                    "raw": root / raw,
                    "raw_sidecar": root / raw.with_suffix(".sha256"),
                }
                paths[kind].unlink()
                clock_calls: list[str] = []
                with self.assertRaises(ValueError):
                    overlay.admit_draftkings_snapshot(
                        root,
                        trace,
                        raw,
                        comparison_clock=lambda: clock_calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                    )
                self.assertEqual(clock_calls, [])
                self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())

    def test_model_authority_rejects_symlink_directory_and_hardlink_leaf_replacements(self) -> None:
        """No authority leaf replacement can be followed or accepted as a regular file."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for replacement in ("symlink", "directory", "hardlink"):
            for kind in ("trace", "report", "report_sidecar", "receipt", "receipt_sidecar"):
                with self.subTest(replacement=replacement, kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                    root = Path(temporary)
                    trace, raw = self._authority(root)
                    receipt = next((root / "audit/experimental_daily_totals/model").glob("*.json"))
                    paths = {
                        "trace": root / trace,
                        "report": root / "reports/experimental_daily_totals/2026-09-01/model.json",
                        "report_sidecar": root / "reports/experimental_daily_totals/2026-09-01/model.sha256",
                        "receipt": receipt,
                        "receipt_sidecar": receipt.with_suffix(".sha256"),
                    }
                    target = paths[kind]
                    target.unlink()
                    attacker: Path | None = None
                    if replacement == "symlink":
                        os.symlink("/dev/null", target)
                    elif replacement == "directory":
                        target.mkdir()
                    else:
                        attacker = root / "attacker-authority.bin"
                        attacker_bytes = b"attacker authority bytes must remain unchanged"
                        attacker.write_bytes(attacker_bytes)
                        os.link(attacker, target)
                    clock_calls: list[str] = []
                    with self.assertRaises(ValueError):
                        overlay.admit_draftkings_snapshot(
                            root,
                            trace,
                            raw,
                            comparison_clock=lambda: clock_calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                        )
                    self.assertEqual(clock_calls, [])
                    if attacker is not None:
                        self.assertEqual(attacker.read_bytes(), attacker_bytes)
                    self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())

    def test_raw_admission_rejects_path_identity_shape_url_and_time_matrix(self) -> None:
        """Catch external capture malformations before the append-only snapshot exists."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        mutations: list[tuple[str, callable]] = [
            ("extra_root", lambda value: {**value, "extra": True}),
            ("missing_root", lambda value: {key: item for key, item in value.items() if key != "games"}),
            ("bad_url", lambda value: {**value, "source_url": "http://sportsbook.draftkings.com/markets"}),
            ("canonical_time", lambda value: {**value, "captured_at_utc": "2026-09-01T14:59:00.0Z"}),
            ("after_bound", lambda value: {**value, "captured_at_utc": "2026-09-01T15:01:00Z"}),
            ("at_start", lambda value: {**value, "captured_at_utc": "2026-09-01T16:00:00Z"}),
            ("unknown_game", lambda value: {**value, "games": [{**value["games"][0], "game_pk": 1}] + value["games"][1:]}),
            ("duplicate_game", lambda value: {**value, "games": value["games"][:-1] + [value["games"][0]]}),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, raw = self._authority(root)
                candidate = self._write_raw(root, mutate(self._raw_value(root, raw)))
                calls: list[object] = []
                with self.assertRaises(ValueError):
                    admit_draftkings_snapshot(root, trace, candidate, comparison_clock=lambda: calls.append(1) or datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
                self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())
                self.assertEqual(calls, [] if name in {"extra_root", "missing_root", "bad_url", "canonical_time", "unknown_game", "duplicate_game"} else [1])
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            with self.assertRaises(ValueError):
                admit_draftkings_snapshot(root, trace, Path("../escape.json"), comparison_clock=lambda: (_ for _ in ()).throw(AssertionError("clock")))
            os.link(root / raw, root / "raw-alias")
            with self.assertRaises(ValueError):
                admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))

    def test_raw_admission_rejects_valid_bytes_under_wrong_content_address(self) -> None:
        """A correct sidecar cannot make raw bytes valid under the wrong 64-hex filename."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            contents = (root / raw).read_bytes()
            digest = hashlib.sha256(contents).hexdigest()
            wrong_digest = "0" * 64 if digest != "0" * 64 else "1" * 64
            wrong = raw.parent / f"{wrong_digest}.json"
            (root / wrong).write_bytes(contents)
            (root / wrong.with_suffix(".sha256")).write_bytes(f"{digest}\n".encode("ascii"))
            calls: list[str] = []
            with self.assertRaises(ValueError):
                admit_draftkings_snapshot(
                    root,
                    trace,
                    wrong,
                    comparison_clock=lambda: calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(calls, [])
            self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())

    def test_raw_admission_rejects_bytes_mutated_after_sidecar(self) -> None:
        """Changing a raw primary after its sidecar was sealed is rejected before timing."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            primary = root / raw
            primary.write_bytes(primary.read_bytes() + b"\nmutated after sidecar")
            calls: list[str] = []
            with self.assertRaises(ValueError):
                admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(calls, [])
            self.assertFalse((root / "audit/experimental_daily_totals_market/draftkings").exists())

    def test_public_model_chain_rejects_individually_valid_cross_bindings(self) -> None:
        """Canonical alternate model objects must not cross the trace/report/receipt boundary."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        cases = (
            "trace_report_path", "trace_report_sha", "trace_receipt_path", "trace_receipt_sha",
            "report_receipt_path", "report_receipt_sha",
        )
        for kind in cases:
            with self.subTest(kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, raw = self._authority(root)
                trace_path = root / trace
                trace_value = json.loads(trace_path.read_bytes())
                original_report = root / Path(trace_value["report_relative"])
                original_receipt = root / Path(trace_value["model_receipt_relative"])
                alternate_report, alternate_report_bytes, _ = self._alternate_model_report(root, trace)
                alternate_receipt, alternate_receipt_bytes, _ = self._alternate_model_receipt(root, trace)

                def write_report(value: dict[str, object]) -> None:
                    value["content_sha256"] = self._digest({key: item for key, item in value.items() if key != "content_sha256"})
                    contents = canonical_json_bytes(value)
                    original_report.write_bytes(contents)
                    original_report.with_suffix(".sha256").write_bytes(hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n")

                def write_trace(value: dict[str, object]) -> None:
                    value["content_sha256"] = self._digest({key: item for key, item in value.items() if key != "content_sha256"})
                    trace_path.write_bytes(canonical_json_bytes(value))

                if kind == "trace_report_path":
                    # The trace is canonical and points at a canonical alternate report,
                    # but its report byte binding remains the original hash.
                    trace_value["report_relative"] = alternate_report.as_posix()
                    write_trace(trace_value)
                elif kind == "trace_report_sha":
                    # Both report bytes and sidecar are exact for the alternate report;
                    # only the trace's sealed report hashes are stale.
                    original_report.write_bytes(alternate_report_bytes)
                    original_report.with_suffix(".sha256").write_bytes(hashlib.sha256(alternate_report_bytes).hexdigest().encode("ascii") + b"\n")
                elif kind == "trace_receipt_path":
                    # Trace points at a canonical alternate receipt while the report
                    # still binds the original receipt path and bytes.
                    trace_value["model_receipt_relative"] = alternate_receipt.as_posix()
                    trace_value["model_receipt_sha256"] = hashlib.sha256(alternate_receipt_bytes).hexdigest()
                    write_trace(trace_value)
                elif kind == "trace_receipt_sha":
                    # The receipt and its sidecar are exact for alternate canonical
                    # bytes, while both sealed consumers retain the original hashes.
                    original_receipt.write_bytes(alternate_receipt_bytes)
                    original_receipt.with_suffix(".sha256").write_bytes(hashlib.sha256(alternate_receipt_bytes).hexdigest().encode("ascii") + b"\n")
                elif kind == "report_receipt_path":
                    report = json.loads(original_report.read_bytes())
                    report["model_receipt_relative"] = alternate_receipt.as_posix()
                    report["model_receipt_sha256"] = hashlib.sha256(alternate_receipt_bytes).hexdigest()
                    write_report(report)
                else:
                    report = json.loads(original_report.read_bytes())
                    report["model_receipt_sha256"] = hashlib.sha256(alternate_receipt_bytes).hexdigest()
                    write_report(report)

                # Every altered primary/sidecar/trace remains canonical and individually
                # schema-valid; rejection must come from the public cross-binding checks.
                from mlb_backtest.daily_totals import (
                    validate_totals_model_receipt, validate_totals_report, validate_totals_trace,
                )

                for primary in (original_report, root / alternate_report, original_receipt, root / alternate_receipt):
                    contents = primary.read_bytes()
                    self.assertEqual(primary.with_suffix(".sha256").read_bytes(), hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n")
                validate_totals_trace(json.loads(trace_path.read_bytes()))
                validate_totals_report(json.loads(original_report.read_bytes()))
                validate_totals_report(json.loads((root / alternate_report).read_bytes()))
                validate_totals_model_receipt(json.loads(original_receipt.read_bytes()))
                validate_totals_model_receipt(json.loads((root / alternate_receipt).read_bytes()))
                with self.assertRaises(ValueError):
                    overlay.load_and_validate_model_authority(root, trace)
                calls: list[str] = []
                with self.assertRaises(ValueError):
                    overlay.admit_draftkings_snapshot(
                        root, trace, raw,
                        comparison_clock=lambda: calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                    )
                self.assertEqual(calls, [])
                self.assertFalse((root / overlay._SNAPSHOT_ROOT).exists())

    def test_build_rejects_recanonicalized_snapshot_with_wrong_raw_byte_length(self) -> None:
        """A self-consistent snapshot cannot lie about the bytes bound by raw_capture_byte_length."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            snapshot_path = root / snapshot
            value = json.loads(snapshot_path.read_bytes())
            value["raw_capture_byte_length"] += 1
            value["content_sha256"] = self._digest({key: item for key, item in value.items() if key != "content_sha256"})
            contents = canonical_json_bytes(value)
            rebound_relative = snapshot.parent / f"{value['content_sha256']}.json"
            snapshot_path.unlink()
            snapshot_path.with_suffix(".sha256").unlink()
            rebound = root / rebound_relative
            rebound.write_bytes(contents)
            rebound.with_suffix(".sha256").write_bytes(overlay._sidecar(contents))
            with self.assertRaises(ValueError):
                overlay.build_overlay(
                    root,
                    trace,
                    rebound_relative,
                    evaluated_clock=lambda: (_ for _ in ()).throw(AssertionError("evaluated clock")),
                )

    def test_public_build_rejects_recanonicalized_cross_bound_snapshot_fields_before_clock(self) -> None:
        """Valid-shape snapshot mutations cannot cross-bind model, raw, or timing evidence."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        cases = (
            "model_trace_path", "model_trace_sha", "model_report_path", "model_report_sha",
            "raw_path", "raw_sha", "raw_length", "captured_and_comparison",
        )
        for kind in cases:
            with self.subTest(kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                alternate_path = "reports/experimental_daily_totals/alternate-model.trace.json"
                alternate_report_path = "reports/experimental_daily_totals/alternate-model.json"
                alternate_raw_path = "data/experimental_daily_totals_market/raw/" + ("f" * 64) + ".json"

                def mutate(value: dict[str, object]) -> None:
                    if kind == "model_trace_path":
                        value["model_trace_relative"] = alternate_path
                    elif kind == "model_trace_sha":
                        value["model_trace_sha256"] = "0" * 64
                    elif kind == "model_report_path":
                        value["model_report_relative"] = alternate_report_path
                    elif kind == "model_report_sha":
                        value["model_report_sha256"] = "0" * 64
                    elif kind == "raw_path":
                        value["raw_capture_relative"] = alternate_raw_path
                    elif kind == "raw_sha":
                        value["raw_capture_sha256"] = "0" * 64
                    elif kind == "raw_length":
                        value["raw_capture_byte_length"] = int(value["raw_capture_byte_length"]) + 1
                    else:
                        # Both timestamps remain individually valid and ordered, but
                        # they no longer match the captured raw payload.
                        value["captured_at_utc"] = "2026-09-01T15:00:00Z"
                        value["comparison_bound_at_utc"] = "2026-09-01T15:00:01Z"

                rebound = self._rewrite_snapshot(root, snapshot, mutate)
                rebound_bytes = (root / rebound).read_bytes()
                self.assertEqual(
                    (root / rebound.with_suffix(".sha256")).read_bytes(),
                    hashlib.sha256(rebound_bytes).hexdigest().encode("ascii") + b"\n",
                )
                validate_draftkings_snapshot(json.loads(rebound_bytes))
                calls: list[str] = []
                with self.assertRaises(ValueError):
                    overlay.build_overlay(
                        root,
                        trace,
                        rebound,
                        evaluated_clock=lambda: calls.append("evaluated") or datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
                    )
                self.assertEqual(calls, [])
                self.assertFalse((root / overlay._OVERLAY_ROOT).exists())

    def test_snapshot_and_evaluated_clock_binding_matrix(self) -> None:
        """Catch normalized evidence mutation before an evaluated clock is consulted."""
        from mlb_backtest.daily_totals_market_overlay import build_overlay

        for kind in ("snapshot", "sidecar"):
            with self.subTest(kind=kind), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                target = root / (snapshot if kind == "snapshot" else snapshot.with_suffix(".sha256"))
                target.write_bytes(b"changed")
                calls: list[object] = []
                with self.assertRaises(ValueError):
                    build_overlay(root, trace, snapshot, evaluated_clock=lambda: calls.append(1) or datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
                self.assertEqual(calls, [])
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            for bad in (datetime(2026, 9, 1, 15, 1), datetime(2026, 9, 1, 15, 1, tzinfo=timezone(timedelta(hours=1))), datetime(2026, 9, 1, 14, 59, tzinfo=timezone.utc)):
                with self.subTest(clock=bad):
                    with self.assertRaises(ValueError):
                        build_overlay(root, trace, snapshot, evaluated_clock=lambda bad=bad: bad)

    def test_build_covers_all_rows_math_and_policy_outcomes(self) -> None:
        """Exercise public build results for NO_QUOTE, push, stale, started, and zero picks."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot, build_overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            value = self._raw_value(root, raw)
            value["games"][0] = {"game_pk": FIXED_GAME_PKS[0], "status": "NO_QUOTE", "reason": "NOT_LISTED"}
            value["games"][1]["line_half_runs"] = 16
            altered = self._write_raw(root, value)
            snapshot = admit_draftkings_snapshot(root, trace, altered, comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc))
            normal = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            repeat = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            self.assertEqual(normal, repeat)
            self.assertEqual([row["game_pk"] for row in normal.report["games"]], list(FIXED_GAME_PKS))
            self.assertEqual(normal.report["games"][0]["reason_codes"], ("NO_QUOTE",))
            self.assertEqual(normal.report["games"][1]["reason_codes"], ("NONPUSH_SUPPORT_TOO_LOW", "SUPPORT_MARGIN_TOO_LOW", "AMBIGUOUS_DIRECTION"))
            stale = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 16, tzinfo=timezone.utc))
            self.assertIn("QUOTE_STALE", stale.report["games"][2]["reason_codes"])
            started = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 16, 1, tzinfo=timezone.utc))
            self.assertIn("GAME_STARTED", started.report["games"][1]["reason_codes"])
            with self.assertRaises(ValueError):
                build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 14, 58, tzinfo=timezone.utc))

    def test_public_quote_age_boundary_is_inclusive_at_900_seconds(self) -> None:
        """The stale gate starts strictly after 900 seconds, including exact boundary age."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot, build_overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(
                root,
                trace,
                raw,
                comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
            )
            exact = build_overlay(
                root,
                trace,
                snapshot,
                evaluated_clock=lambda: datetime(2026, 9, 1, 15, 14, tzinfo=timezone.utc),
            )
            beyond = build_overlay(
                root,
                trace,
                snapshot,
                evaluated_clock=lambda: datetime(2026, 9, 1, 15, 14, 1, tzinfo=timezone.utc),
            )
            self.assertNotIn("QUOTE_STALE", exact.report["games"][0]["reason_codes"])
            self.assertIn("QUOTE_STALE", beyond.report["games"][0]["reason_codes"])

    def test_public_game_started_boundary_is_inclusive_at_scheduled_start(self) -> None:
        """A game is started at its scheduled instant, but not one microsecond before it."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot, build_overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(
                root,
                trace,
                raw,
                comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
            )
            before = build_overlay(
                root,
                trace,
                snapshot,
                evaluated_clock=lambda: datetime(2026, 9, 1, 15, 59, 59, 999999, tzinfo=timezone.utc),
            )
            exact = build_overlay(
                root,
                trace,
                snapshot,
                evaluated_clock=lambda: datetime(2026, 9, 1, 16, tzinfo=timezone.utc),
            )
            self.assertNotIn("GAME_STARTED", before.report["games"][0]["reason_codes"])
            self.assertIn("GAME_STARTED", exact.report["games"][0]["reason_codes"])

    def test_append_only_order_conflict_fault_and_rebind_matrix(self) -> None:
        """Catch publication reordered before trace, conflicting, or falsely successful."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            original = overlay._existing_or_write
            written: list[str] = []

            def record(*args: object, **kwargs: object) -> object:
                written.append(args[3])
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "_existing_or_write", side_effect=record):
                paths = overlay.persist_overlay(root, built)
            self.assertEqual(written, ["overlay report sidecar", "overlay report", "overlay Markdown", "overlay trace"])
            original_bytes = [(root / path).read_bytes() for path in paths]
            self.assertEqual(overlay.persist_overlay(root, built), paths)
            self.assertEqual([(root / path).read_bytes() for path in paths], original_bytes)
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            (root / built.report_sidecar_relative).parent.mkdir(parents=True, exist_ok=True)
            (root / built.report_sidecar_relative).write_bytes(b"conflict")
            authority_before = self._model_authority_bytes(root, trace)
            with self.assertRaises(FileExistsError):
                overlay.persist_overlay(root, built)
            self.assertFalse((root / built.trace_relative).exists())
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_snapshot_admission_writes_sidecar_before_primary(self) -> None:
        """Admission commits the market snapshot sidecar before its primary JSON."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            original = overlay._existing_or_write
            labels: list[str] = []

            def record(*args: object, **kwargs: object) -> object:
                labels.append(args[3])
                return original(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "_existing_or_write", side_effect=record):
                overlay.admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(labels, ["market snapshot sidecar", "market snapshot"])

    def test_identical_snapshot_admission_is_byte_exact_and_creates_no_new_paths(self) -> None:
        """Repeated admission of fixed evidence returns the same path without appending files."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            def comparison() -> datetime:
                return datetime(2026, 9, 1, 15, tzinfo=timezone.utc)

            first = admit_draftkings_snapshot(root, trace, raw, comparison_clock=comparison)
            namespace = root / "audit/experimental_daily_totals_market/draftkings"
            before = {path.relative_to(root): path.read_bytes() for path in namespace.rglob("*") if path.is_file()}
            second = admit_draftkings_snapshot(root, trace, raw, comparison_clock=comparison)
            after = {path.relative_to(root): path.read_bytes() for path in namespace.rglob("*") if path.is_file()}
            self.assertEqual(second, first)
            self.assertEqual(after, before)

    def test_snapshot_admission_rejects_conflicting_existing_sidecar(self) -> None:
        """A conflicting existing snapshot sidecar fails without replacing evidence."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
            sidecar = root / snapshot.with_suffix(".sha256")
            sidecar.write_bytes(b"conflicting sidecar")
            before = {path.relative_to(root): path.read_bytes() for path in (root / snapshot).parent.rglob("*") if path.is_file()}
            with self.assertRaises(FileExistsError):
                admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
            after = {path.relative_to(root): path.read_bytes() for path in (root / snapshot).parent.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_snapshot_admission_rejects_conflicting_existing_primary(self) -> None:
        """A conflicting existing snapshot JSON fails after preserving its bytes."""
        from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            snapshot = admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
            primary = root / snapshot
            primary.write_bytes(b"conflicting primary")
            before = {path.relative_to(root): path.read_bytes() for path in primary.parent.rglob("*") if path.is_file()}
            with self.assertRaises(FileExistsError):
                admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
            after = {path.relative_to(root): path.read_bytes() for path in primary.parent.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_snapshot_admission_rebind_failure_after_writes_preserves_partial_evidence(self) -> None:
        """A post-write admission rebind fault raises and leaves the committed prefix intact."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            from mlb_backtest.daily_totals_market_overlay import admit_draftkings_snapshot

            expected_snapshot = admit_draftkings_snapshot(
                root,
                trace,
                raw,
                comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
            )
            expected_primary = (root / expected_snapshot).read_bytes()
            expected_sidecar = (root / expected_snapshot.with_suffix(".sha256")).read_bytes()
            (root / expected_snapshot).unlink()
            (root / expected_snapshot.with_suffix(".sha256")).unlink()
            original = overlay.rebind_regular_snapshot
            rebind_labels: list[str] = []

            def fail_after_writes(trusted: object, evidence: object, label: str, **kwargs: object) -> None:
                rebind_labels.append(label)
                if label.startswith("snapshot admission:"):
                    raise ValueError("injected post-write rebind failure")
                original(trusted, evidence, label, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "rebind_regular_snapshot", side_effect=fail_after_writes):
                with self.assertRaises(ValueError):
                    overlay.admit_draftkings_snapshot(
                        root,
                        trace,
                        raw,
                        comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                    )
            post_write_labels = [label for label in rebind_labels if label.startswith("snapshot admission:")]
            self.assertEqual(len(post_write_labels), 1)
            namespace = root / "audit/experimental_daily_totals_market/draftkings"
            files = sorted(path for path in namespace.glob("*") if path.is_file())
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0].suffix, ".json")
            self.assertEqual(files[1].suffix, ".sha256")
            self.assertEqual((root / expected_snapshot).read_bytes(), expected_primary)
            self.assertEqual((root / expected_snapshot.with_suffix(".sha256")).read_bytes(), expected_sidecar)

    def test_valid_admission_never_opens_the_source_url(self) -> None:
        """Admission consumes local bytes only, even when the capture records a URL."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network access")) as urlopen, patch(
                "socket.create_connection", side_effect=AssertionError("network access")
            ) as create_connection:
                snapshot = overlay.admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertTrue((root / snapshot).is_file())
            urlopen.assert_not_called()
            create_connection.assert_not_called()

    def test_comparison_clock_runs_once_only_after_authority_and_raw_validation(self) -> None:
        """Admission validates authority/raw first, then clocks immediately around snapshot assembly."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            (root / trace).unlink()
            missing_authority_calls: list[str] = []
            with self.assertRaises(ValueError):
                overlay.admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: missing_authority_calls.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(missing_authority_calls, [])

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            original_raw_capture = overlay._raw_capture
            original_clock = overlay._clock
            original_snapshot_validator = overlay.validate_draftkings_snapshot
            events: list[str] = []

            def record_raw(*args: object, **kwargs: object) -> object:
                events.append("raw")
                return original_raw_capture(*args, **kwargs)  # type: ignore[arg-type]

            def record_clock(*args: object, **kwargs: object) -> object:
                events.append("clock")
                return original_clock(*args, **kwargs)  # type: ignore[arg-type]

            def record_snapshot(value: object) -> dict[str, object]:
                events.append("snapshot")
                return original_snapshot_validator(value)

            with patch.object(overlay, "_raw_capture", side_effect=record_raw), patch.object(
                overlay, "_clock", side_effect=record_clock
            ), patch.object(overlay, "validate_draftkings_snapshot", side_effect=record_snapshot):
                overlay.admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(events, ["raw", "clock", "snapshot"])

    def test_valid_admission_comparison_clock_runs_once_after_authority(self) -> None:
        """A valid admission clocks exactly once, after the model authority is loaded."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            original_authority = overlay.load_and_validate_model_authority
            original_raw = overlay._raw_capture
            original_snapshot_validator = overlay.validate_draftkings_snapshot
            events: list[str] = []

            def record_authority(*args: object, **kwargs: object) -> object:
                events.append("authority")
                return original_authority(*args, **kwargs)  # type: ignore[arg-type]

            def record_raw(*args: object, **kwargs: object) -> object:
                events.append("raw")
                return original_raw(*args, **kwargs)  # type: ignore[arg-type]

            def record_snapshot(value: object) -> dict[str, object]:
                events.append("snapshot")
                return original_snapshot_validator(value)

            with patch.object(overlay, "load_and_validate_model_authority", side_effect=record_authority), patch.object(
                overlay, "_raw_capture", side_effect=record_raw
            ), patch.object(overlay, "validate_draftkings_snapshot", side_effect=record_snapshot):
                overlay.admit_draftkings_snapshot(
                    root,
                    trace,
                    raw,
                    comparison_clock=lambda: events.append("comparison") or datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
                )
            self.assertEqual(events, ["authority", "raw", "comparison", "snapshot"])

    def test_valid_build_evaluated_clock_runs_once_after_model_and_snapshot_validation(self) -> None:
        """Build clocks once after both public inputs validate, and never for invalid inputs."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            original_authority = overlay.load_and_validate_model_authority
            original_snapshot_loader = overlay._load_snapshot
            events: list[str] = []

            def record_authority(*args: object, **kwargs: object) -> object:
                events.append("authority")
                return original_authority(*args, **kwargs)  # type: ignore[arg-type]

            def record_snapshot(*args: object, **kwargs: object) -> object:
                events.append("snapshot")
                return original_snapshot_loader(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "load_and_validate_model_authority", side_effect=record_authority), patch.object(
                overlay, "_load_snapshot", side_effect=record_snapshot
            ):
                overlay.build_overlay(
                    root,
                    trace,
                    snapshot,
                    evaluated_clock=lambda: events.append("evaluated") or datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
                )
            self.assertEqual(events, ["authority", "snapshot", "evaluated"])

        for invalid_authority, invalid_snapshot in (True, False), (False, True):
            with self.subTest(invalid_authority=invalid_authority, invalid_snapshot=invalid_snapshot), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                if invalid_authority:
                    self._rewrite_model_report(root, trace, lambda report: report["games"][0].update({"residual_count": 763, "residual_sha256": "0" * 64}))
                if invalid_snapshot:
                    snapshot = self._rewrite_snapshot(root, snapshot, lambda value: value.update({"raw_capture_byte_length": int(value["raw_capture_byte_length"]) + 1}))
                calls: list[str] = []
                with self.assertRaises(ValueError):
                    overlay.build_overlay(
                        root,
                        trace,
                        snapshot,
                        evaluated_clock=lambda: calls.append("evaluated") or datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
                    )
                self.assertEqual(calls, [])
                self.assertFalse((root / overlay._OVERLAY_ROOT).exists())

    def test_built_overlay_is_frozen_slotted_and_model_evidence_immutable(self) -> None:
        """Catch a mutable transport or an overlay write that changes model evidence."""
        from mlb_backtest.daily_totals_market_overlay import build_overlay, persist_overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw, snapshot = self._admitted(root)
            report = root / "reports/experimental_daily_totals/2026-09-01/model.json"
            receipt = next((root / "audit/experimental_daily_totals/model").glob("*.json"))
            authority_paths = [root / trace, report, report.with_suffix(".sha256"), receipt, receipt.with_suffix(".sha256")]
            before = [path.read_bytes() for path in authority_paths]
            built = build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            self.assertFalse(hasattr(built, "__dict__"))
            self.assertIsInstance(built.report_bytes, bytes)
            self.assertIsInstance(built.markdown_bytes, bytes)
            self.assertIsInstance(built.trace_bytes, bytes)
            with self.assertRaises(FrozenInstanceError):
                built.report_bytes = b"changed"  # type: ignore[misc]
            persist_overlay(root, built)
            self.assertEqual([path.read_bytes() for path in authority_paths], before)

    def test_public_api_rejects_symlink_and_directory_file_attacks(self) -> None:
        """Exercise no-follow rejection at raw, snapshot, and all overlay writer leaves."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            raw_path = root / raw
            raw_path.unlink()
            os.symlink("/dev/null", raw_path)
            with self.assertRaises(ValueError):
                overlay.admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
        for leaf in ("snapshot", "snapshot_sidecar"):
            with self.subTest(leaf=leaf), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                path = root / (snapshot if leaf == "snapshot" else snapshot.with_suffix(".sha256"))
                path.unlink()
                os.symlink("/dev/null", path)
                with self.assertRaises(ValueError):
                    overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            authority_before = self._model_authority_bytes(root, trace)
            for leaf in (built.report_sidecar_relative, built.report_relative, built.markdown_relative, built.trace_relative):
                with self.subTest(leaf=leaf.name):
                    path = root / leaf
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink("/dev/null", path)
                    with self.assertRaises(ValueError):
                        overlay.persist_overlay(root, built)
                    self.assertEqual(self._model_authority_bytes(root, trace), authority_before)
                    path.unlink()

    def test_write_failures_preserve_pretrace_evidence_and_never_return_success(self) -> None:
        """Each writer fault leaves exactly the successfully committed prefix."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        labels = ("overlay report sidecar", "overlay report", "overlay Markdown", "overlay trace")
        for failing in ("overlay report sidecar", "overlay report", "overlay Markdown", "overlay trace"):
            with self.subTest(failing=failing), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
                authority_before = self._model_authority_bytes(root, trace)
                original = overlay._existing_or_write
                calls: list[str] = []

                def fail(*args: object, **kwargs: object) -> object:
                    calls.append(args[3])
                    if args[3] == failing:
                        raise OSError("injected")
                    return original(*args, **kwargs)  # type: ignore[arg-type]

                with patch.object(overlay, "_existing_or_write", side_effect=fail):
                    with self.assertRaises(OSError):
                        overlay.persist_overlay(root, built)
                self.assertEqual(calls, list(labels[:labels.index(failing) + 1]))
                targets = (
                    built.report_sidecar_relative,
                    built.report_relative,
                    built.markdown_relative,
                    built.trace_relative,
                )
                expected_bytes = (
                    overlay._sidecar(built.report_bytes),
                    built.report_bytes,
                    built.markdown_bytes,
                    built.trace_bytes,
                )
                for index, (target, expected) in enumerate(zip(targets, expected_bytes)):
                    path = root / target
                    if index <= labels.index(failing) - 1:
                        self.assertTrue(path.is_file())
                        self.assertEqual(path.read_bytes(), expected)
                    else:
                        self.assertFalse(path.exists())
                self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_pretrace_rebind_failure_preserves_output_prefix_without_trace(self) -> None:
        """A pre-trace input rebind fault preserves sidecar, JSON, and Markdown only."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            authority_before = self._model_authority_bytes(root, trace)
            target = f"overlay pre-trace: {built.report_sidecar_relative.as_posix()}"
            original = overlay.rebind_regular_snapshot
            calls: list[str] = []

            def fail(trusted: object, evidence: object, label: str, **kwargs: object) -> None:
                calls.append(label)
                if label == target:
                    raise ValueError("identity instability")
                original(trusted, evidence, label, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "rebind_regular_snapshot", side_effect=fail):
                with self.assertRaises(ValueError):
                    overlay.persist_overlay(root, built)
            self.assertEqual(calls[-1], target)
            for relative, expected in (
                (built.report_sidecar_relative, overlay._sidecar(built.report_bytes)),
                (built.report_relative, built.report_bytes),
                (built.markdown_relative, built.markdown_bytes),
            ):
                self.assertEqual((root / relative).read_bytes(), expected)
            self.assertFalse((root / built.trace_relative).exists())
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_posttrace_rebind_failure_preserves_all_four_exact_artifacts(self) -> None:
        """A final post-trace rebind fault leaves the complete committed bundle."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
            authority_before = self._model_authority_bytes(root, trace)
            target = f"overlay post-trace: {built.trace_relative.as_posix()}"
            original = overlay.rebind_regular_snapshot
            calls: list[str] = []

            def fail(trusted: object, evidence: object, label: str, **kwargs: object) -> None:
                calls.append(label)
                if label == target:
                    raise ValueError("identity instability")
                original(trusted, evidence, label, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "rebind_regular_snapshot", side_effect=fail):
                with self.assertRaises(ValueError):
                    overlay.persist_overlay(root, built)
            self.assertEqual(calls[-1], target)
            self.assertEqual(calls.count(target), 1)
            for relative, expected in (
                (built.report_sidecar_relative, overlay._sidecar(built.report_bytes)),
                (built.report_relative, built.report_bytes),
                (built.markdown_relative, built.markdown_bytes),
                (built.trace_relative, built.trace_bytes),
            ):
                self.assertEqual((root / relative).read_bytes(), expected)
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_output_hardlinks_are_rejected_without_mutating_attacker_bytes(self) -> None:
        """Each output leaf rejects an attacker hardlink and leaves its source untouched."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for index, target_name in enumerate(("report_sidecar_relative", "report_relative", "markdown_relative", "trace_relative")):
            with self.subTest(leaf=target_name), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
                authority_before = self._model_authority_bytes(root, trace)
                target = root / getattr(built, target_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                attacker = root / f"attacker-{index}.bin"
                attacker_bytes = b"attacker bytes must remain unchanged"
                attacker.write_bytes(attacker_bytes)
                os.link(attacker, target)
                with self.assertRaises(ValueError):
                    overlay.persist_overlay(root, built)
                self.assertEqual(attacker.read_bytes(), attacker_bytes)
                self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_public_apis_reject_actual_directories_at_input_and_output_leaves(self) -> None:
        """Actual directories at every raw, normalized, and output leaf are rejected."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        for leaf in ("raw", "raw sidecar"):
            with self.subTest(leaf=leaf), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, raw = self._authority(root)
                target = root / (raw if leaf == "raw" else raw.with_suffix(".sha256"))
                target.unlink()
                target.mkdir()
                with self.assertRaises(ValueError):
                    overlay.admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc))

        for leaf in ("snapshot", "snapshot sidecar"):
            with self.subTest(leaf=leaf), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                target = root / (snapshot if leaf == "snapshot" else snapshot.with_suffix(".sha256"))
                target.unlink()
                target.mkdir()
                with self.assertRaises(ValueError):
                    overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))

        targets_builder = (
            "report_sidecar_relative", "report_relative", "markdown_relative", "trace_relative",
        )
        for attribute in targets_builder:
            with self.subTest(leaf=attribute), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
                authority_before = self._model_authority_bytes(root, trace)
                target = root / getattr(built, attribute)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.mkdir()
                with self.assertRaises(ValueError):
                    overlay.persist_overlay(root, built)
                self.assertTrue(target.is_dir())
                self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_rebind_identity_instability_is_rejected_at_each_public_boundary(self) -> None:
        """Injected rebind instability fails admission, build, pre-trace, and post-trace stages."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        original = overlay.rebind_regular_snapshot

        def fail_at(target: str, operation: object) -> list[str]:
            calls: list[str] = []

            def fail(trusted: object, evidence: object, label: str, **kwargs: object) -> None:
                calls.append(label)
                if label == target:
                    raise ValueError("identity instability")
                original(trusted, evidence, label, **kwargs)  # type: ignore[arg-type]

            with patch.object(overlay, "rebind_regular_snapshot", side_effect=fail):
                with self.assertRaises(ValueError):
                    operation()
            self.assertEqual(calls[-1], target)
            return calls

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            authority_before = self._model_authority_bytes(root, trace)

            admission_target = f"snapshot admission inputs: {raw.as_posix()}"
            fail_at(admission_target, lambda: overlay.admit_draftkings_snapshot(root, trace, raw, comparison_clock=lambda: datetime(2026, 9, 1, 15, tzinfo=timezone.utc)))
            self.assertFalse((root / overlay._SNAPSHOT_ROOT).exists())
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, _, snapshot = self._admitted(root)
            authority_before = self._model_authority_bytes(root, trace)
            target = f"overlay build: {snapshot.as_posix()}"
            calls = fail_at(target, lambda: overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc)))
            self.assertEqual(calls[-1], target)
            self.assertFalse((root / overlay._OVERLAY_ROOT).exists())
            self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

        for final_stage in (False, True):
            with self.subTest(final_stage=final_stage), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace, _, snapshot = self._admitted(root)
                built = overlay.build_overlay(root, trace, snapshot, evaluated_clock=lambda: datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc))
                authority_before = self._model_authority_bytes(root, trace)
                target = (
                    f"overlay post-trace: {built.trace_relative.as_posix()}"
                    if final_stage else f"overlay pre-trace: {built.report_sidecar_relative.as_posix()}"
                )
                fail_at(target, lambda: overlay.persist_overlay(root, built))
                expected = (
                    (built.report_sidecar_relative, overlay._sidecar(built.report_bytes)),
                    (built.report_relative, built.report_bytes),
                    (built.markdown_relative, built.markdown_bytes),
                    (built.trace_relative, built.trace_bytes),
                )
                if final_stage:
                    for relative, contents in expected:
                        self.assertEqual((root / relative).read_bytes(), contents)
                else:
                    for relative, contents in expected[:3]:
                        self.assertEqual((root / relative).read_bytes(), contents)
                    self.assertFalse((root / built.trace_relative).exists())
                self.assertEqual(self._model_authority_bytes(root, trace), authority_before)

    def test_public_unique_over_pick_has_exact_support_and_margin(self) -> None:
        """A fresh admitted capture can publish an exact OVER research pick."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        def report_over(report: dict[str, object]) -> None:
            game = report["games"][0]
            samples = [9] * 764
            game.update({
                "residual_samples": samples,
                "residual_sha256": self._digest(samples),
                "interval_80": [9, 9],
            })

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            _, _, built = self._build_public_case(root, mutate_report=report_over)
            row = built.report["games"][0]
            self.assertEqual(row["selected_side"], "OVER")
            self.assertEqual(row["decision"], "RESEARCH_PICK")
            self.assertEqual(row["reason_codes"], ())
            self.assertEqual((row["over_support"], row["under_support"], row["push_support"]), (1.0, 0.0, 0.0))
            self.assertEqual((row["over_conditional_support"], row["under_conditional_support"]), (1.0, 0.0))
            self.assertEqual((row["over_support_margin"], row["under_support_margin"]), (0.5, -0.5))
            paths = persist_overlay(root, built)
            self.assertTrue((root / paths[-1]).is_file())

    def test_public_unique_under_pick_has_exact_support_and_margin(self) -> None:
        """A fresh admitted capture can publish an exact UNDER research pick."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            _, _, built = self._build_public_case(root)
            row = built.report["games"][0]
            self.assertEqual(row["selected_side"], "UNDER")
            self.assertEqual(row["decision"], "RESEARCH_PICK")
            self.assertEqual(row["reason_codes"], ())
            self.assertEqual((row["over_support"], row["under_support"], row["push_support"]), (0.0, 1.0, 0.0))
            self.assertEqual((row["over_conditional_support"], row["under_conditional_support"]), (0.0, 1.0))
            self.assertEqual((row["over_support_margin"], row["under_support_margin"]), (-0.5, 0.5))
            paths = persist_overlay(root, built)
            self.assertTrue((root / paths[-1]).is_file())

    def test_public_policy_reasons_are_isolated_when_attainable(self) -> None:
        """Public builds expose each single policy failure with canonical ordering."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        cases = (
            (
                "NONPUSH_SUPPORT_TOO_LOW",
                lambda value: value["games"][0].update({"line_half_runs": 16, "over_american_odds": 100, "under_american_odds": 100}),
                lambda report: report["games"][0].update({
                    "residual_samples": [9] * 600 + [7] * 4 + [8] * 160,
                    "residual_sha256": self._digest([9] * 600 + [7] * 4 + [8] * 160),
                    "interval_80": [8, 9],
                }),
                ("NONPUSH_SUPPORT_TOO_LOW",),
            ),
            (
                "POINT_GAP_TOO_SMALL",
                lambda value: value["games"][0].update({"line_half_runs": 19, "over_american_odds": 100, "under_american_odds": 100}),
                None,
                ("POINT_GAP_TOO_SMALL",),
            ),
            (
                "SUPPORT_MARGIN_TOO_LOW",
                lambda value: value["games"][0].update({"line_half_runs": 17, "over_american_odds": -110, "under_american_odds": -110}),
                lambda report: report["games"][0].update({
                    "residual_samples": [9] * 401 + [8] * 363,
                    "residual_sha256": self._digest([9] * 401 + [8] * 363),
                    "interval_80": [8, 9],
                }),
                ("SUPPORT_MARGIN_TOO_LOW",),
            ),
        )
        for name, raw_mutator, report_mutator, expected_reasons in cases:
            with self.subTest(name=name), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                _, _, built = self._build_public_case(root, raw_mutator, report_mutator)
                row = built.report["games"][0]
                self.assertEqual(row["decision"], "PASS")
                self.assertEqual(row["reason_codes"], expected_reasons)
                paths = persist_overlay(root, built)
                self.assertTrue((root / paths[-1]).is_file())

    def test_public_ambiguous_direction_reports_required_support_margin_companion(self) -> None:
        """A tied public direction is ambiguous and has no selected margin to pass."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        samples = [9] * 382 + [8] * 382

        def report_tied(report: dict[str, object]) -> None:
            report["games"][0].update({
                "residual_samples": samples,
                "residual_sha256": self._digest(samples),
                "interval_80": [8, 9],
            })

        def raw_tied(value: dict[str, object]) -> None:
            value["games"][0].update({"line_half_runs": 17, "over_american_odds": 100, "under_american_odds": 100})

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            _, _, built = self._build_public_case(root, raw_tied, report_tied)
            row = built.report["games"][0]
            self.assertEqual(row["selected_side"], None)
            self.assertEqual(row["decision"], "PASS")
            # Build's selected-margin gate necessarily accompanies ambiguity;
            # the semantic validator also accepts AMBIGUOUS_DIRECTION alone.
            self.assertEqual(row["reason_codes"], ("SUPPORT_MARGIN_TOO_LOW", "AMBIGUOUS_DIRECTION"))
            paths = persist_overlay(root, built)
            self.assertTrue((root / paths[-1]).is_file())

    def test_public_combined_policy_failures_use_canonical_reason_order(self) -> None:
        """All attainable policy failures remain in the fixed reason-code order."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        def raw_combined(value: dict[str, object]) -> None:
            value["games"][0].update({"line_half_runs": 20, "over_american_odds": 100, "under_american_odds": 100})

        def report_combined(report: dict[str, object]) -> None:
            samples = [10] * 764
            report["games"][0].update({
                "residual_samples": samples,
                "residual_sha256": self._digest(samples),
                "interval_80": [10, 10],
            })

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            _, _, built = self._build_public_case(root, raw_combined, report_combined)
            row = built.report["games"][0]
            self.assertEqual(row["decision"], "PASS")
            self.assertEqual(row["reason_codes"], (
                "NONPUSH_SUPPORT_TOO_LOW", "POINT_GAP_TOO_SMALL",
                "SUPPORT_MARGIN_TOO_LOW", "AMBIGUOUS_DIRECTION",
            ))
            paths = persist_overlay(root, built)
            self.assertTrue((root / paths[-1]).is_file())

    def test_public_zero_pick_persist_is_idempotent_without_new_filename(self) -> None:
        """A no-quote overlay persists twice byte-identically at one path set."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        def raw_zero_pick(value: dict[str, object]) -> None:
            value["games"] = [
                {"game_pk": game["game_pk"], "status": "NO_QUOTE", "reason": "NOT_LISTED"}
                for game in value["games"]
            ]

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            _, _, built = self._build_public_case(root, raw_zero_pick)
            self.assertEqual({row["decision"] for row in built.report["games"]}, {"PASS"})
            self.assertTrue(all("selected_side" not in row for row in built.report["games"]))
            first_paths = persist_overlay(root, built)
            first_bytes = [path.read_bytes() for path in (root / p for p in first_paths)]
            files_after_first = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
            second_paths = persist_overlay(root, built)
            second_bytes = [path.read_bytes() for path in (root / p for p in second_paths)]
            files_after_second = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
            self.assertEqual(second_paths, first_paths)
            self.assertEqual(second_bytes, first_bytes)
            self.assertEqual(files_after_second, files_after_first)

    def test_model_residual_count_hash_corruption_rejects_before_evaluated_clock(self) -> None:
        """Corrupt model residual authority is rejected before build's evaluated clock."""
        import mlb_backtest.daily_totals_market_overlay as overlay

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace, raw = self._authority(root)
            candidate = self._write_raw(root, self._raw_value(root, raw))
            snapshot = overlay.admit_draftkings_snapshot(
                root, trace, candidate,
                comparison_clock=lambda: datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
            )

            def corrupt(report: dict[str, object]) -> None:
                report["games"][0]["residual_count"] = 763
                report["games"][0]["residual_sha256"] = "0" * 64

            self._rewrite_model_report(root, trace, corrupt)
            calls: list[object] = []
            with self.assertRaises(ValueError):
                overlay.build_overlay(
                    root, trace, snapshot,
                    evaluated_clock=lambda: calls.append(1) or datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc),
                )
            self.assertEqual(calls, [])
            self.assertFalse((root / "reports/experimental_daily_totals_overlay").exists())

    def test_public_nearest_attainable_nonpush_thresholds_are_distinct(self) -> None:
        """With 764 samples, 611/764 is below .80 while 612/764 is above it."""
        from mlb_backtest.daily_totals_market_overlay import persist_overlay

        for nonpush, expected_reasons, under_count, push_count in (
            (611, ("NONPUSH_SUPPORT_TOO_LOW",), 11, 153),
            (612, (), 12, 152),
        ):
            with self.subTest(nonpush=nonpush), TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                samples = [9] * 600 + [7] * under_count + [8] * push_count

                def raw_threshold(value: dict[str, object]) -> None:
                    value["games"][0].update({"line_half_runs": 16, "over_american_odds": 100, "under_american_odds": 100})

                def report_threshold(report: dict[str, object]) -> None:
                    report["games"][0].update({
                        "residual_samples": samples,
                        "residual_sha256": self._digest(samples),
                        "interval_80": [8, 9],
                    })

                _, _, built = self._build_public_case(root, raw_threshold, report_threshold)
                row = built.report["games"][0]
                self.assertEqual(row["reason_codes"], tuple(expected_reasons))
                self.assertEqual(row["selected_side"], "OVER")
                paths = persist_overlay(root, built)
                self.assertTrue((root / paths[-1]).is_file())

    def test_public_nearest_attainable_support_margin_thresholds_cover_both_directions(self) -> None:
        """With 764 samples, exact .55 support is unreachable (764*.55 = 420.2), so 420/421 straddle it."""
        cases = (
            ("OVER", 16, 10.0, ([9, 7], [7, 9])),
            ("UNDER", 20, 8.0, ([9, 11], [9, 11])),
        )
        for direction, line_half_runs, expected_total, (sample_values, interval_hint) in cases:
            for winning_count in (420, 421):
                with self.subTest(direction=direction, winning_count=winning_count), TemporaryDirectory(dir=Path.cwd()) as temporary:
                    root = Path(temporary)
                    winning, losing = sample_values
                    samples = [winning] * winning_count + [losing] * (764 - winning_count)

                    def raw_threshold(value: dict[str, object]) -> None:
                        value["games"][0].update({
                            "line_half_runs": line_half_runs,
                            "over_american_odds": 100,
                            "under_american_odds": 100,
                        })

                    def report_threshold(report: dict[str, object]) -> None:
                        report["games"][0].update({
                            "away_expected_runs": expected_total / 2,
                            "home_expected_runs": expected_total / 2,
                            "expected_total": expected_total,
                            "residual_samples": samples,
                            "residual_sha256": self._digest(samples),
                            "interval_80": list(interval_hint),
                        })

                    _, _, built = self._build_public_case(root, raw_threshold, report_threshold)
                    row = built.report["games"][0]
                    self.assertEqual(row["selected_side"], direction)
                    if winning_count == 420:
                        self.assertEqual(row["reason_codes"], ("SUPPORT_MARGIN_TOO_LOW",))
                    else:
                        self.assertEqual(row["reason_codes"], ())
                        self.assertEqual(row["decision"], "RESEARCH_PICK")

    def test_public_point_gap_boundary_is_inclusive_for_over_and_under(self) -> None:
        """A one-run gap passes; the nearest float below one run receives POINT_GAP_TOO_SMALL."""
        cases = (
            ("OVER", 16, 9.0, 8.999999),
            ("UNDER", 20, 9.0, 9.000001),
        )
        for direction, line_half_runs, passing_total, failing_total in cases:
            for expected_total, expected_reasons in ((passing_total, ()), (failing_total, ("POINT_GAP_TOO_SMALL",))):
                with self.subTest(direction=direction, expected_total=expected_total), TemporaryDirectory(dir=Path.cwd()) as temporary:
                    root = Path(temporary)
                    samples = [9] * 764

                    def raw_threshold(value: dict[str, object]) -> None:
                        value["games"][0].update({
                            "line_half_runs": line_half_runs,
                            "over_american_odds": 100,
                            "under_american_odds": 100,
                        })

                    def report_threshold(report: dict[str, object]) -> None:
                        report["games"][0].update({
                            "away_expected_runs": expected_total / 2,
                            "home_expected_runs": expected_total / 2,
                            "expected_total": expected_total,
                            "residual_samples": samples,
                            "residual_sha256": self._digest(samples),
                            "interval_80": [9, 9],
                        })

                    _, _, built = self._build_public_case(root, raw_threshold, report_threshold)
                    row = built.report["games"][0]
                    self.assertEqual(row["selected_side"], direction)
                    self.assertEqual(row["reason_codes"], expected_reasons)
                    if not expected_reasons:
                        self.assertEqual(row["decision"], "RESEARCH_PICK")


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
