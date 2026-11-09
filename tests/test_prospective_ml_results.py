"""Hermetic end-to-end contracts for manual official-result collection."""

from __future__ import annotations

import json
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.live_top3_contracts import LiveHttpObservation, canonical_json_bytes
from mlb_backtest.live_sources import _persist_observation
from mlb_backtest.market_play_contracts import canonical_json_bytes as settlement_json_bytes
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
)
from mlb_backtest.prospective_ml_settlement import (
    build_calibration_report,
    build_settlement,
    load_official_result_evidence,
    persist_calibration_report,
    persist_settlement,
)
from mlb_backtest.prospective_ml_results import CollectedResults, CollectionFailure, ResultDisposition, collect_results, main
import mlb_backtest.prospective_ml_results as prospective_ml_results
from tests.prospective_ml_fixtures import GAME_PK, sealed_ml_fixture


def _clock() -> datetime:
    return datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)


def _official_payload(
    *,
    game_pk: int = GAME_PK,
    official_date: str = "2026-09-03",
    away_team: str = "Away Club",
    home_team: str = "Home Club",
    abstract_state: str = "Final",
    detailed_state: str = "Final",
    home_won: bool = True,
) -> bytes:
    return canonical_json_bytes(
        {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": game_pk,
                            "officialDate": official_date,
                            "status": {
                                "abstractGameState": abstract_state,
                                "detailedState": detailed_state,
                            },
                            "teams": {
                                "away": {
                                    "team": {"name": away_team},
                                    "isWinner": not home_won,
                                },
                                "home": {
                                    "team": {"name": home_team},
                                    "isWinner": home_won,
                                },
                            },
                        }
                    ]
                }
            ]
        }
    )


def _http(url: str, payload: bytes, *, final_url: str | None = None) -> LiveHttpObservation:
    return LiveHttpObservation(
        requested_url=url,
        final_url=url if final_url is None else final_url,
        started_at_utc="2026-09-03T22:59:00Z",
        response_completed_at_utc="2026-09-03T23:00:00Z",
        http_status=200,
        content_type="application/json",
        etag=None,
        last_modified=None,
        body=payload,
    )


def _pending_report(root: Path) -> Path:
    trace = sealed_ml_fixture(root)
    built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
    persisted = persist_ml_observations(
        root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
    )
    return persist_calibration_report(
        root, build_calibration_report(root, persisted.primary_claim_paths, [])
    )


def _add_second_primary(root: Path, report: Path) -> Path:
    """Add an independently authenticated synthetic primary for failure recovery."""
    report_value = json.loads((root / report).read_text())
    first_claim = Path(report_value["primary_claims"][0])
    claim_value = json.loads((root / first_claim).read_text())
    snapshot = json.loads((root / str(claim_value["candidate_relative"])).read_text())
    snapshot["game_pk"] = GAME_PK + 1
    snapshot["away_team"] = "Away Two"
    snapshot["home_team"] = "Home Two"
    snapshot["content_sha256"] = hashlib.sha256(
        settlement_json_bytes({key: value for key, value in snapshot.items() if key != "content_sha256"})
    ).hexdigest()
    raw = settlement_json_bytes(snapshot)
    candidate = Path(
        "audit/prospective_ml_observation/candidates/2026-09-03/777002/"
        + "a" * 64
        + f"/{snapshot['content_sha256']}.json"
    )
    (root / candidate).parent.mkdir(parents=True, exist_ok=True)
    (root / candidate).write_bytes(raw)
    (root / candidate.with_suffix(".json.sha256")).write_bytes(
        f"{hashlib.sha256(raw).hexdigest()}\n".encode()
    )
    claim = Path("audit/prospective_ml_observation/primary_claims/777002/" + "a" * 64 + ".json")
    claim_value = {
        "schema": "PROSPECTIVE_ML_PRIMARY_CLAIM_V1",
        "game_pk": GAME_PK + 1,
        "model_card_sha256": "a" * 64,
        "candidate_relative": candidate.as_posix(),
        "candidate_sha256": hashlib.sha256(raw).hexdigest(),
        "claimed_at_utc": "2026-09-03T19:59:59Z",
    }
    (root / claim).parent.mkdir(parents=True, exist_ok=True)
    (root / claim).write_bytes(settlement_json_bytes(claim_value))
    return persist_calibration_report(
        root, build_calibration_report(root, [first_claim, claim], [])
    )


class ProspectiveMlResultsTests(unittest.TestCase):
    def test_final_result_persists_receipt_head_and_hand_derived_metrics(self) -> None:
        """A missing FINAL branch would leave no immutable settlement or score."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _pending_report(root)
            requested: list[str] = []

            def fetcher(source: str, url: str, **_: object) -> LiveHttpObservation:
                requested.append(url)
                return _http(url, _official_payload())

            result = collect_results(root, report, fetcher=fetcher, durable_clock=_clock)

            self.assertEqual(len(requested), 1)
            self.assertEqual(result.dispositions[0].state, "FINAL")
            self.assertIsNotNone(result.dispositions[0].receipt_relative)
            self.assertIsNotNone(result.dispositions[0].head_relative)
            value = json.loads((root / result.report_relative).read_text())
            self.assertEqual(value["pending_count"], 0)
            self.assertEqual(value["metrics"]["all_primary"]["n"], 1)
            self.assertEqual(value["metrics"]["all_primary"]["accuracy"], "1.000000")
            self.assertEqual(value["metrics"]["all_primary"]["brier"], "0.090000")

    def test_nonfinal_result_is_captured_but_remains_pending(self) -> None:
        """Treating a postponed game as final would fabricate a calibration row."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _pending_report(root)

            result = collect_results(
                root,
                report,
                fetcher=lambda _source, url, **_: _http(
                    url,
                    _official_payload(
                        abstract_state="Preview", detailed_state="Postponed"
                    ),
                ),
                durable_clock=_clock,
            )

            self.assertEqual(result.dispositions[0].state, "POSTPONED")
            self.assertIsNotNone(result.dispositions[0].receipt_relative)
            self.assertIsNone(result.dispositions[0].head_relative)
            value = json.loads((root / result.report_relative).read_text())
            self.assertEqual(value["pending_count"], 1)
            self.assertEqual(value["metrics"]["all_primary"]["n"], 0)

    def test_tampered_report_is_rejected_before_http(self) -> None:
        """Removing report authentication must never make a network request."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _pending_report(root)
            value = json.loads((root / report).read_text())
            value["pending_count"] = 99
            (root / report).write_bytes(canonical_json_bytes(value))
            calls = 0

            def fetcher(_source: str, _url: str, **_: object) -> LiveHttpObservation:
                nonlocal calls
                calls += 1
                raise AssertionError("must not fetch")

            with self.assertRaises(ValueError):
                collect_results(root, report, fetcher=fetcher, durable_clock=_clock)
            self.assertEqual(calls, 0)

    def test_identity_mismatch_preserves_durable_receipt_and_never_writes_head(self) -> None:
        """A wrong team/date/game response must fail closed after evidence capture."""
        cases = (
            {"game_pk": GAME_PK + 1},
            {"official_date": "2026-09-04"},
            {"away_team": "Wrong Away"},
            {"home_team": "Wrong Home"},
        )
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                report = _pending_report(root)
                with self.assertRaises(ValueError):
                    collect_results(
                        root,
                        report,
                        fetcher=lambda _source, url, **_: _http(url, _official_payload(**options)),
                        durable_clock=_clock,
                    )
                self.assertTrue(list((root / "audit/experimental_daily/live_receipts").glob("*.json")))
                self.assertFalse(list((root / "audit/prospective_ml_observation/settlement_heads").rglob("*.json")))

    def test_returned_terminal_report_makes_zero_requests_and_stale_input_rejects(self) -> None:
        """Fetching a settled game again would create an unauthorized revision."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            stale = _pending_report(root)
            collected = collect_results(
                root,
                stale,
                fetcher=lambda _source, url, **_: _http(url, _official_payload()),
                durable_clock=_clock,
            )
            calls = 0

            def forbidden(_source: str, _url: str, **_: object) -> LiveHttpObservation:
                nonlocal calls
                calls += 1
                raise AssertionError("terminal reports must not fetch")

            repeated = collect_results(root, collected.report_relative, fetcher=forbidden, durable_clock=_clock)
            self.assertEqual(calls, 0)
            self.assertEqual(repeated.dispositions, ())
            with self.assertRaises(ValueError):
                collect_results(root, stale, fetcher=forbidden, durable_clock=_clock)
            self.assertEqual(calls, 0)

    def test_malformed_response_and_redirect_mismatch_fail_without_a_head(self) -> None:
        """Transport/result validation failures cannot be reported as a settled pass."""
        cases = (
            lambda url: _http(url, b"not json"),
            lambda url: replace(_http(url, _official_payload()), http_status=503),
            lambda url: _http(url, _official_payload(), final_url="https://statsapi.mlb.com/api/v1/other"),
        )
        for factory in cases:
            with self.subTest(factory=factory), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                report = _pending_report(root)
                with self.assertRaises(CollectionFailure):
                    collect_results(root, report, fetcher=lambda _source, url, **_: factory(url), durable_clock=_clock)
                self.assertFalse(list((root / "audit/prospective_ml_observation/settlement_heads").rglob("*.json")))

    def test_raw_helper_manifest_source_or_query_tampering_is_rejected(self) -> None:
        """The helper-manifest compatibility branch still binds its exact query/source."""
        for field, value in (("query", {"game_pk": 999999}), ("source", "other_source")):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                report = _pending_report(root)
                collected = collect_results(
                    root,
                    report,
                    fetcher=lambda _source, url, **_: _http(url, _official_payload()),
                    durable_clock=_clock,
                )
                receipt = Path(collected.dispositions[0].receipt_relative or "")
                receipt_value = json.loads((root / receipt).read_text())
                manifest = root / str(receipt_value["manifest_relative"])
                manifest_value = json.loads(manifest.read_text())
                manifest_value[field] = value
                manifest_raw = json.dumps(manifest_value, ensure_ascii=False, sort_keys=True, indent=2).encode()
                manifest.write_bytes(manifest_raw)
                seal = manifest.with_name(manifest.name[:-5] + ".sha256")
                seal.write_bytes(f"{hashlib.sha256(manifest_raw).hexdigest()}\n".encode())
                receipt_value["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
                receipt_raw = canonical_json_bytes(receipt_value)
                (root / receipt).write_bytes(receipt_raw)
                (root / receipt.with_suffix(".completion.json")).write_bytes(
                    canonical_json_bytes(
                        {
                            "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                            "durable_at_utc": "2026-09-03T23:00:00Z",
                        }
                    )
                )
                with self.assertRaises(ValueError):
                    load_official_result_evidence(root, receipt)

    def test_raw_helper_receipt_role_and_completion_shape_are_authenticated(self) -> None:
        """A resealed helper receipt cannot downgrade FINAL_SCHEDULE or its completion."""
        for mutation in ("binding_role", "completion"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                report = _pending_report(root)
                collected = collect_results(
                    root,
                    report,
                    fetcher=lambda _source, url, **_: _http(url, _official_payload()),
                    durable_clock=_clock,
                )
                receipt = Path(collected.dispositions[0].receipt_relative or "")
                receipt_value = json.loads((root / receipt).read_text())
                completion = receipt.with_suffix(".completion.json")
                completion_value = json.loads((root / completion).read_text())
                if mutation == "binding_role":
                    receipt_value["binding_role"] = "LIVE_SCHEDULE"
                    receipt_raw = canonical_json_bytes(receipt_value)
                    (root / receipt).write_bytes(receipt_raw)
                    completion_value["receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
                    completion_raw = canonical_json_bytes(completion_value)
                else:
                    completion_raw = canonical_json_bytes(
                        {
                            "receipt_sha256": hashlib.sha256((root / receipt).read_bytes()).hexdigest(),
                            "durable_at_utc": completion_value["durable_at_utc"],
                        }
                    )
                (root / completion).write_bytes(completion_raw)
                (root / receipt.with_suffix(".completion.sha256")).write_bytes(
                    f"{hashlib.sha256(completion_raw).hexdigest()}\n".encode()
                )
                with self.assertRaises(ValueError):
                    load_official_result_evidence(root, receipt)

    def test_helper_result_receipt_rejects_noncanonical_schedule_url(self) -> None:
        """A self-consistent helper artifact must still use the collector's exact URL."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            url = "https://statsapi.mlb.com/api/v1/schedule?gamePks=777001"
            persisted = _persist_observation(
                root,
                raw_source="mlb_official_result",
                source="stats_api",
                binding_role="FINAL_SCHEDULE",
                game_pks=(GAME_PK,),
                observation_id="alternate-url",
                query={
                    "game_pk": GAME_PK,
                    "kind": "prospective_ml_official_result",
                    "observation_id": "alternate-url",
                    "url": url,
                },
                http=_http(url, _official_payload()),
                durable_clock=_clock,
            )
            with self.assertRaises(ValueError):
                load_official_result_evidence(root, persisted.source_binding.receipt_relative)

    def test_partial_final_persists_a_recoverable_updated_report_on_later_failure(self) -> None:
        """Discarding an earlier durable head when another game fails loses audit evidence."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _add_second_primary(root, _pending_report(root))

            def fetcher(_source: str, url: str, **_: object) -> LiveHttpObservation:
                if "gamePks=777001" in url:
                    return _http(url, _official_payload())
                return _http(url, b"not json")

            with self.assertRaises(CollectionFailure) as raised:
                collect_results(root, report, fetcher=fetcher, durable_clock=_clock)
            partial = raised.exception.result
            self.assertIsNotNone(partial)
            assert partial is not None
            self.assertEqual([item.state for item in partial.dispositions], ["FINAL", "FAILED"])
            value = json.loads((root / partial.report_relative).read_text())
            self.assertEqual(value["metrics"]["all_primary"]["n"], 1)
            self.assertEqual(value["pending_count"], 1)

    def test_report_write_failure_keeps_known_partial_paths_in_failure(self) -> None:
        """Suppressing a partial-report write error must not suppress an earlier head."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _add_second_primary(root, _pending_report(root))

            def fetcher(_source: str, url: str, **_: object) -> LiveHttpObservation:
                return _http(url, _official_payload()) if "777001" in url else _http(url, b"not json")

            with patch.object(prospective_ml_results, "persist_calibration_report", side_effect=OSError("report disk full")):
                with self.assertRaises(CollectionFailure) as raised:
                    collect_results(root, report, fetcher=fetcher, durable_clock=_clock)
            partial = raised.exception.result
            self.assertIsNotNone(partial)
            assert partial is not None
            self.assertIsNone(partial.report_relative)
            self.assertEqual(partial.report_error, "report disk full")
            self.assertEqual([item.state for item in partial.dispositions], ["FINAL", "FAILED"])
            self.assertIsNotNone(partial.dispositions[0].head_relative)
            self.assertTrue((root / str(partial.dispositions[0].head_relative)).is_file())

    def test_settlement_write_failure_keeps_receipt_and_never_reports_success(self) -> None:
        """A settlement write error leaves captured evidence inspectable without a head."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _pending_report(root)
            original = (root / report).read_bytes()
            with patch.object(prospective_ml_results, "persist_settlement", side_effect=OSError("head disk full")):
                with self.assertRaises(CollectionFailure) as raised:
                    collect_results(
                        root, report, fetcher=lambda _source, url, **_: _http(url, _official_payload()), durable_clock=_clock
                    )
            partial = raised.exception.result
            self.assertIsNotNone(partial)
            assert partial is not None
            self.assertEqual((root / report).read_bytes(), original)
            self.assertEqual(partial.dispositions[0].state, "FAILED")
            self.assertIsNotNone(partial.dispositions[0].receipt_relative)
            self.assertIsNone(partial.dispositions[0].head_relative)
            self.assertTrue((root / str(partial.dispositions[0].receipt_relative)).is_file())

    def test_final_report_write_failure_exposes_durable_head_through_api_and_cli(self) -> None:
        """A final report disk failure must not discard an already committed Final head."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = _pending_report(root)
            with patch.object(prospective_ml_results, "persist_calibration_report", side_effect=OSError("report disk full")):
                with self.assertRaises(CollectionFailure) as raised:
                    collect_results(
                        root, report, fetcher=lambda _source, url, **_: _http(url, _official_payload()), durable_clock=_clock
                    )
            partial = raised.exception.result
            self.assertIsNotNone(partial)
            assert partial is not None
            self.assertIsNone(partial.report_relative)
            self.assertEqual(partial.report_error, "report disk full")
            self.assertEqual(partial.dispositions[0].state, "FINAL")
            self.assertTrue((root / str(partial.dispositions[0].head_relative)).is_file())

        recovered = CollectedResults(
            None,
            (ResultDisposition(GAME_PK, "snapshot.json", "FINAL", "receipt.json", "head.json", None),),
            "report disk full",
        )
        output = io.StringIO()
        with patch.object(prospective_ml_results, "collect_results", side_effect=CollectionFailure("report disk full", recovered)), redirect_stdout(output):
            self.assertEqual(main(["collect", "--root", "unused", "--report", "input.json"]), 1)
        value = json.loads(output.getvalue())
        self.assertEqual(value["partial"]["report_relative"], None)
        self.assertEqual(value["partial"]["report_error"], "report disk full")
        self.assertEqual(value["partial"]["dispositions"][0]["head_relative"], "head.json")

    def test_supplied_pending_head_rejects_before_http(self) -> None:
        """A manual pending settlement must use explicit settle --prior, never collection."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            initial = _pending_report(root)
            captured = collect_results(
                root,
                initial,
                fetcher=lambda _source, url, **_: _http(
                    url, _official_payload(abstract_state="Preview", detailed_state="Scheduled")
                ),
                durable_clock=_clock,
            )
            snapshot = Path(json.loads((root / initial).read_text())["primary_claims"][0])
            claim = json.loads((root / snapshot).read_text())
            pending = persist_settlement(
                root,
                build_settlement(root, claim["candidate_relative"], captured.dispositions[0].receipt_relative or ""),
            )
            report = persist_calibration_report(
                root,
                build_calibration_report(root, [snapshot], [pending.head_path]),
            )
            calls = 0

            def forbidden(_source: str, _url: str, **_: object) -> LiveHttpObservation:
                nonlocal calls
                calls += 1
                raise AssertionError("must not fetch")

            with self.assertRaises(ValueError):
                collect_results(root, report, fetcher=forbidden, durable_clock=_clock)
            self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
