from __future__ import annotations

import dataclasses
import io
import os
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    V52EvidenceStore,
    logical_query_id,
)
from mlb_backtest.v52_http import (
    RetryPolicy,
    deterministic_jitter,
    fetch_limits,
)


T0 = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"


def _query(*, statcast: bool = False, single_day: bool = False) -> LogicalQuery:
    if statcast:
        end = "2024-04-01" if single_day else "2024-04-02"
        return LogicalQuery(
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            parameters={},
            requested_range={
                "end_date": end,
                "start_date": "2024-04-01",
                "year": 2024,
            },
            expected_media_type="text/csv",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
    return LogicalQuery(
        source="venue_team_timezone_table",
        grain="effective_dated_venue_team_reference",
        endpoint="https://statsapi.mlb.com/api/v1/teams",
        parameters={"season": 2024, "sportId": 1},
        requested_range={"year": 2024},
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _capture(
    body: bytes | None = b"{}",
    *,
    status: int | None = 200,
    error: str | None = None,
    retry_after: str | None = None,
    timed_out: bool = False,
) -> AttemptCapture:
    headers = () if retry_after is None else (("Retry-After", retry_after),)
    final_url = None if status is None else "https://statsapi.mlb.com/api/v1/teams"
    return AttemptCapture(
        started_at_utc="2026-07-15T12:00:00.000000Z",
        ended_at_utc="2026-07-15T12:00:00.000000Z",
        final_url=final_url,
        http_status=status,
        response_headers=headers,
        body=body,
        error_classification=error,
        timed_out=timed_out,
    )


def _timeout(*, partial: bool = False) -> AttemptCapture:
    if partial:
        return dataclasses.replace(
            _capture(b"partial", error="TRANSIENT_NETWORK", timed_out=True),
            final_url="https://baseballsavant.mlb.com/statcast_search/csv",
        )
    return _capture(
        None,
        status=None,
        error="TRANSIENT_NETWORK",
        timed_out=True,
    )


def _ready(_raw: bytes, _query: LogicalQuery) -> Mapping[str, object]:
    return {"status": "READY"}


class ScriptedFetcher:
    def __init__(self, captures: list[AttemptCapture], events: list[str] | None = None):
        self._captures = iter(captures)
        self.calls: list[tuple[float, int]] = []
        self.events = events

    def __call__(
        self, query: LogicalQuery, *, timeout: float, max_bytes: int
    ) -> AttemptCapture:
        self.calls.append((timeout, max_bytes))
        if self.events is not None:
            self.events.append("fetch")
        return next(self._captures)


class ManualClock:
    def __init__(self, now: datetime = T0):
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def _acquire(
    store: V52EvidenceStore,
    query: LogicalQuery,
    parser: Callable[[bytes, LogicalQuery], Mapping[str, object]],
    fetcher: ScriptedFetcher,
    *,
    before_network: Callable[[LogicalQuery], None] = lambda _query: None,
    clock: ManualClock | None = None,
):
    from mlb_backtest.v52_acquire import acquire_with_retries

    selected_clock = ManualClock() if clock is None else clock
    return acquire_with_retries(
        store,
        query,
        parser,
        policy=RetryPolicy(),
        limits=fetch_limits(query),
        before_network=before_network,
        fetcher=fetcher,
        sleeper=selected_clock.sleep,
        clock=selected_clock,
    )


class V52AcquireRetryEngineTest(unittest.TestCase):
    def make_store(self, directory: str) -> V52EvidenceStore:
        return V52EvidenceStore(Path(directory).resolve() / "raw")

    def test_public_typed_outcomes_and_success_use_exact_transport_limits(self) -> None:
        from mlb_backtest.v52_acquire import (
            SplitRequired,
            TerminalAcquisitionFailure,
        )

        self.assertTrue(SplitRequired.__dataclass_params__.frozen)
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(SplitRequired)),
            ("reason", "exhausted_attempt_number"),
        )
        self.assertTrue(issubclass(SplitRequired, Exception))
        self.assertTrue(issubclass(TerminalAcquisitionFailure, Exception))
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            fetcher = ScriptedFetcher([_capture()])
            evidence = _acquire(store, query, _ready, fetcher)

            self.assertEqual(fetcher.calls, [(90.0, 16_777_216)])
            self.assertEqual(evidence, store.verify_success(query))

    def test_gate_precedes_every_fetch_and_transient_is_durable_before_sleep(
        self,
    ) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            fetcher = ScriptedFetcher(
                [_capture(b"busy", status=503, error="TRANSIENT_HTTP"), _capture()],
                events,
            )

            def gate(_query: LogicalQuery) -> None:
                store.read_verified_query_progress(query)
                events.append("gate")

            def sleep(_seconds: float) -> None:
                progress = store.read_verified_query_progress(query)
                self.assertEqual(
                    tuple(item["outcome"] for item in progress.attempt_receipts),
                    ("TRANSIENT_HTTP",),
                )
                events.append("sleep")
                manual_clock.sleep(_seconds)

            from mlb_backtest.v52_acquire import acquire_with_retries

            errors: list[BaseException] = []
            manual_clock = ManualClock()

            def run() -> None:
                try:
                    acquire_with_retries(
                        store,
                        query,
                        _ready,
                        policy=RetryPolicy(),
                        limits=fetch_limits(query),
                        before_network=gate,
                        fetcher=fetcher,
                        sleeper=sleep,
                        clock=manual_clock,
                    )
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(
                worker.is_alive(), "retry engine held its lease across gate or sleep"
            )
            if errors:
                raise errors[0]
            self.assertEqual(events, ["gate", "fetch", "sleep", "gate", "fetch"])

    def test_resume_pacing_uses_persisted_deadline_and_only_remaining_sleep(
        self,
    ) -> None:
        query = _query()
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with store.acquire_query(query) as lease:
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(
                        b"busy",
                        status=503,
                        error="TRANSIENT_HTTP",
                        retry_after="10",
                    ),
                    outcome="TRANSIENT_HTTP",
                )
            clock = ManualClock()

            def partial_sleep(seconds: float) -> None:
                self.assertEqual(seconds, 10.0)
                clock.now += timedelta(seconds=4)
                raise RuntimeError("crashed during sleep")

            from mlb_backtest.v52_acquire import acquire_with_retries

            with self.assertRaisesRegex(RuntimeError, "crashed during sleep"):
                acquire_with_retries(
                    store,
                    query,
                    _ready,
                    policy=RetryPolicy(),
                    limits=fetch_limits(query),
                    before_network=lambda _query: self.fail("gate before pacing"),
                    fetcher=ScriptedFetcher([]),
                    sleeper=partial_sleep,
                    clock=clock,
                )

            fetcher = ScriptedFetcher([_capture()])
            gates: list[str] = []
            _acquire(
                store,
                query,
                _ready,
                fetcher,
                before_network=lambda _query: gates.append("gate"),
                clock=clock,
            )
            self.assertEqual(clock.sleeps, [6.0])
            self.assertEqual(gates, ["gate"])
            self.assertEqual(len(fetcher.calls), 1)

    def test_resume_pacing_uses_http_date_and_skips_past_deadline(self) -> None:
        from email.utils import format_datetime

        for seconds_past_end, expected_sleep in ((3, 8.0), (12, None)):
            with (
                self.subTest(seconds_past_end=seconds_past_end),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query()
                with store.acquire_query(query) as lease:
                    store.record_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(
                            b"busy",
                            status=503,
                            error="TRANSIENT_HTTP",
                            retry_after=format_datetime(
                                T0 + timedelta(seconds=11), usegmt=True
                            ),
                        ),
                        outcome="TRANSIENT_HTTP",
                    )
                clock = ManualClock(T0 + timedelta(seconds=seconds_past_end))
                _acquire(
                    store,
                    query,
                    _ready,
                    ScriptedFetcher([_capture()]),
                    clock=clock,
                )
                self.assertEqual(
                    clock.sleeps,
                    [] if expected_sleep is None else [expected_sleep],
                )

    def test_resume_pacing_uses_retry_ordinal_for_base_and_attempt_for_jitter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            with store.acquire_query(query) as lease:
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"busy", status=503, error="TRANSIENT_HTTP"),
                    outcome="TRANSIENT_HTTP",
                )
                self.assertEqual(store.next_attempt_number(lease), 2)
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(b"busy", status=503, error="TRANSIENT_HTTP"),
                    outcome="TRANSIENT_HTTP",
                )
            clock = ManualClock()
            _acquire(
                store,
                query,
                _ready,
                ScriptedFetcher([_capture()]),
                clock=clock,
            )
            expected = deterministic_jitter(
                logical_query_id(query),
                3,
                2,
                RetryPolicy().jitter_fraction,
            )
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], expected, places=6)

    def test_current_process_pacing_seeds_jitter_with_new_attempt_number(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            old_transient = dataclasses.replace(
                _capture(b"busy", status=503, error="TRANSIENT_HTTP"),
                started_at_utc="2026-07-15T11:59:50.000000Z",
                ended_at_utc="2026-07-15T11:59:50.000000Z",
            )
            with store.acquire_query(query) as lease:
                store.record_attempt(
                    query,
                    store.next_attempt_number(lease),
                    old_transient,
                    outcome="TRANSIENT_HTTP",
                )
                self.assertEqual(store.next_attempt_number(lease), 2)
            clock = ManualClock()
            fetcher = ScriptedFetcher(
                [
                    _capture(b"busy", status=503, error="TRANSIENT_HTTP"),
                    _capture(),
                ]
            )
            _acquire(store, query, _ready, fetcher, clock=clock)
            expected = deterministic_jitter(
                logical_query_id(query),
                3,
                2,
                RetryPolicy().jitter_fraction,
            )
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], expected, places=6)

    def test_retry_after_delta_date_and_invalid_fall_back_to_maximum_delay(
        self,
    ) -> None:
        from email.utils import format_datetime

        cases = (
            ("9", 9.0),
            (format_datetime(T0 + timedelta(seconds=11), usegmt=True), 11.0),
            ("not-a-delay", None),
        )
        for retry_after, expected in cases:
            with (
                self.subTest(retry_after=retry_after),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query()
                clock = ManualClock()
                _acquire(
                    store,
                    query,
                    _ready,
                    ScriptedFetcher(
                        [
                            _capture(
                                b"busy",
                                status=503,
                                error="TRANSIENT_HTTP",
                                retry_after=retry_after,
                            ),
                            _capture(),
                        ]
                    ),
                    clock=clock,
                )
                jitter = deterministic_jitter(
                    logical_query_id(query), 1, 1, RetryPolicy().jitter_fraction
                )
                self.assertEqual(len(clock.sleeps), 1)
                self.assertAlmostEqual(
                    clock.sleeps[0], max(expected or 0.0, jitter), places=6
                )

    def test_timeout_budget_persists_partial_split_and_raises_typed_outcome(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import SplitRequired

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query(statcast=True)
            fetcher = ScriptedFetcher([_timeout(partial=True) for _ in range(6)])
            clock = ManualClock()
            with (
                patch("time.sleep", side_effect=AssertionError("real sleep forbidden")),
                self.assertRaises(SplitRequired) as raised,
            ):
                _acquire(store, query, _ready, fetcher, clock=clock)

            self.assertEqual(raised.exception.reason, "TIMEOUT")
            self.assertEqual(raised.exception.exhausted_attempt_number, 6)
            self.assertEqual(len(clock.sleeps), 5)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                raised.exception.reason = "ROW_CAP"  # type: ignore[misc]
            progress = store.read_verified_query_progress(query)
            self.assertEqual(
                tuple(item["outcome"] for item in progress.attempt_receipts),
                ("TRANSIENT_NETWORK",) * 5 + ("SPLIT_REQUIRED",),
            )
            split = store.read_verified_split_attempt(query, 6)
            self.assertEqual(split.body_path.read_bytes(), b"partial")

    def test_timeout_exhaustion_quarantines_single_day_and_non_statcast_queries(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import TerminalAcquisitionFailure

        for query in (_query(statcast=True, single_day=True), _query()):
            with (
                self.subTest(source=query.source),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                fetcher = ScriptedFetcher([_timeout() for _ in range(6)])
                with self.assertRaises(TerminalAcquisitionFailure) as raised:
                    _acquire(store, query, _ready, fetcher)
                self.assertEqual(raised.exception.classification, "TIMEOUT")
                progress = store.read_verified_query_progress(query)
                self.assertEqual(progress.status, "QUARANTINED")
                self.assertEqual(
                    progress.attempt_receipts[-1]["outcome"], "QUARANTINED"
                )
                self.assertEqual(
                    progress.attempt_receipts[-1]["error_classification"],
                    "TIMEOUT",
                )
                self.assertEqual(len(fetcher.calls), 6)

    def test_resume_counts_prior_transients_but_never_reuses_dangling_reservation(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import SplitRequired

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query(statcast=True)
            with store.acquire_query(query) as lease:
                for _ in range(2):
                    number = store.next_attempt_number(lease)
                    store.record_attempt(
                        query,
                        number,
                        _timeout(),
                        outcome="TRANSIENT_NETWORK",
                    )
                self.assertEqual(store.next_attempt_number(lease), 3)

            fetcher = ScriptedFetcher([_timeout() for _ in range(3)])
            with self.assertRaises(SplitRequired) as raised:
                _acquire(store, query, _ready, fetcher)

            self.assertEqual(raised.exception.exhausted_attempt_number, 6)
            self.assertEqual(len(fetcher.calls), 3)
            progress = store.read_verified_query_progress(query)
            self.assertEqual(progress.pending_attempt_numbers, (3,))
            self.assertEqual(
                tuple(item["attempt_number"] for item in progress.attempt_receipts),
                (1, 2, 4, 5, 6),
            )

    def test_competing_reservation_or_receipt_forces_restitch_and_regate(self) -> None:
        for competing_receipt in (False, True):
            with (
                self.subTest(competing_receipt=competing_receipt),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query()
                gate_calls = 0
                writer_errors: list[BaseException] = []

                def gate(_query: LogicalQuery) -> None:
                    nonlocal gate_calls
                    gate_calls += 1
                    if gate_calls != 1:
                        return

                    def write_competing_state() -> None:
                        try:
                            with store.acquire_query(query) as lease:
                                number = store.next_attempt_number(lease)
                                if competing_receipt:
                                    store.record_attempt(
                                        query,
                                        number,
                                        _capture(
                                            b"busy",
                                            status=503,
                                            error="TRANSIENT_HTTP",
                                        ),
                                        outcome="TRANSIENT_HTTP",
                                    )
                        except BaseException as exc:
                            writer_errors.append(exc)

                    writer = threading.Thread(target=write_competing_state)
                    writer.start()
                    writer.join(2)
                    self.assertFalse(writer.is_alive(), "competing writer blocked")
                    if writer_errors:
                        raise writer_errors[0]

                fetcher = ScriptedFetcher([_capture()])
                _acquire(
                    store,
                    query,
                    _ready,
                    fetcher,
                    before_network=gate,
                )
                self.assertEqual(gate_calls, 2)
                self.assertEqual(len(fetcher.calls), 1)
                progress = store.read_verified_query_progress(query)
                self.assertEqual(progress.status, "SUCCESS")
                self.assertEqual(
                    tuple(item["attempt_number"] for item in progress.attempt_receipts),
                    (1, 2) if competing_receipt else (2,),
                )
                self.assertEqual(
                    progress.pending_attempt_numbers,
                    () if competing_receipt else (1,),
                )

    def test_policy_timeout_must_match_frozen_contract_even_with_matching_limits(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import acquire_with_retries

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            policy = RetryPolicy(timeout=12)
            fetcher = ScriptedFetcher([_capture()])
            with self.assertRaisesRegex(ValueError, "request timeout|frozen"):
                acquire_with_retries(
                    store,
                    query,
                    _ready,
                    policy=policy,
                    limits=fetch_limits(query, policy=policy),
                    before_network=lambda _query: None,
                    fetcher=fetcher,
                    sleeper=lambda _seconds: None,
                    clock=lambda: T0,
                )
            self.assertEqual(fetcher.calls, [])

    def test_six_reserved_slots_stop_before_gate_reservation_or_fetch(self) -> None:
        from mlb_backtest.v52_acquire import (
            TerminalAcquisitionFailure,
            acquire_with_retries,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            with store.acquire_query(query) as lease:
                self.assertEqual(
                    tuple(store.next_attempt_number(lease) for _ in range(6)),
                    (1, 2, 3, 4, 5, 6),
                )
            gates: list[str] = []
            fetcher = ScriptedFetcher([])
            with self.assertRaises(TerminalAcquisitionFailure) as raised:
                acquire_with_retries(
                    store,
                    query,
                    _ready,
                    policy=RetryPolicy(),
                    limits=fetch_limits(query),
                    before_network=lambda _query: gates.append("gate"),
                    fetcher=fetcher,
                    sleeper=lambda _seconds: None,
                    clock=lambda: T0,
                )
            self.assertEqual(
                raised.exception.classification,
                "RETRY_BUDGET_ALREADY_EXHAUSTED",
            )
            self.assertEqual(gates, [])
            self.assertEqual(fetcher.calls, [])
            with store.acquire_query(query) as lease:
                self.assertEqual(
                    store.read_leased_attempt_budget(lease).reservation_numbers,
                    (1, 2, 3, 4, 5, 6),
                )

    def test_size_and_row_cap_split_multi_day_but_quarantine_single_day(self) -> None:
        from mlb_backtest.v52_acquire import (
            SplitRequired,
            TerminalAcquisitionFailure,
        )

        cases = (
            (
                "SIZE_LIMIT",
                _capture(b"partial", error="MAX_BYTES_EXCEEDED"),
                _ready,
            ),
            (
                "ROW_CAP",
                _capture(b"capped"),
                lambda _raw, _query: {"split_disposition": "SPLIT_REQUIRED"},
            ),
        )
        for reason, capture, parser in cases:
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query(statcast=True)
                with self.assertRaises(SplitRequired) as raised:
                    _acquire(store, query, parser, ScriptedFetcher([capture]))
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(raised.exception.exhausted_attempt_number, 1)

            with (
                self.subTest(reason=reason, single_day=True),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query(statcast=True, single_day=True)
                with self.assertRaises(TerminalAcquisitionFailure):
                    _acquire(store, query, parser, ScriptedFetcher([capture]))
                self.assertEqual(
                    store.read_verified_query_progress(query).status,
                    "QUARANTINED",
                )

    def test_permanent_bodyful_bodyless_and_semantic_failures_quarantine_once(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import TerminalAcquisitionFailure

        def invalid(_raw: bytes, _query: LogicalQuery) -> Mapping[str, object]:
            raise ValueError("bad source semantics")

        cases = (
            (
                _capture(b"not found", status=404, error="PERMANENT_HTTP"),
                _ready,
            ),
            (
                _capture(None, status=None, error="PERMANENT_NETWORK"),
                _ready,
            ),
            (_capture(b"malformed"), invalid),
        )
        for capture, parser in cases:
            with (
                self.subTest(error=capture.error_classification),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = self.make_store(directory)
                query = _query()
                fetcher = ScriptedFetcher([capture])
                with self.assertRaises(TerminalAcquisitionFailure):
                    _acquire(store, query, parser, fetcher)
                progress = store.read_verified_query_progress(query)
                self.assertEqual(progress.status, "QUARANTINED")
                self.assertEqual(len(progress.attempt_receipts), 1)
                self.assertEqual(len(fetcher.calls), 1)

    def test_success_and_prior_terminal_states_never_fetch_sleep_or_rewrite(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            SplitRequired,
            TerminalAcquisitionFailure,
        )

        for state in ("SUCCESS", "QUARANTINED", "SPLIT_REQUIRED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store = self.make_store(directory)
                query = _query(statcast=state == "SPLIT_REQUIRED")
                if state == "SUCCESS":
                    expected = _acquire(
                        store, query, _ready, ScriptedFetcher([_capture()])
                    )
                else:
                    with store.acquire_query(query) as lease:
                        number = store.next_attempt_number(lease)
                        if state == "QUARANTINED":
                            store.quarantine_attempt(
                                query,
                                number,
                                _capture(b"bad", status=404, error="PERMANENT_HTTP"),
                                error_classification="PERMANENT_HTTP",
                            )
                        else:
                            store.persist_split_attempt(
                                query,
                                number,
                                dataclasses.replace(
                                    _capture(b"partial", error="SIZE_LIMIT"),
                                    final_url="https://baseballsavant.mlb.com/statcast_search/csv",
                                ),
                                reason="SIZE_LIMIT",
                            )
                    expected = None
                before = {
                    path.relative_to(store.root): (
                        path.read_bytes(),
                        path.stat().st_mtime_ns,
                    )
                    for path in store.root.rglob("*")
                    if path.is_file()
                }
                fetcher = ScriptedFetcher([])
                clock = ManualClock()
                if state == "SUCCESS":
                    self.assertEqual(
                        _acquire(store, query, _ready, fetcher, clock=clock), expected
                    )
                else:
                    error = (
                        SplitRequired
                        if state == "SPLIT_REQUIRED"
                        else TerminalAcquisitionFailure
                    )
                    with self.assertRaises(error):
                        _acquire(store, query, _ready, fetcher, clock=clock)
                after = {
                    path.relative_to(store.root): (
                        path.read_bytes(),
                        path.stat().st_mtime_ns,
                    )
                    for path in store.root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(fetcher.calls, [])
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(after, before)

    def test_owned_pending_failure_recovers_without_network(self) -> None:
        from mlb_backtest.v52_acquire import TerminalAcquisitionFailure

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            with store.acquire_query(query) as lease:
                with (
                    patch.object(
                        store,
                        "_finish_failure_transaction",
                        side_effect=RuntimeError("interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    store.quarantine_bodyless_attempt(
                        query,
                        store.next_attempt_number(lease),
                        _capture(None, status=None, error="PERMANENT_NETWORK"),
                        error_classification="PERMANENT_NETWORK",
                    )

            fetcher = ScriptedFetcher([])
            with self.assertRaises(TerminalAcquisitionFailure):
                _acquire(store, query, _ready, fetcher)
            self.assertEqual(fetcher.calls, [])
            self.assertEqual(
                store.read_verified_query_progress(query).status, "QUARANTINED"
            )

    def test_pre_network_gate_and_programmer_error_propagate_without_relabeling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()
            fetcher = ScriptedFetcher([_capture()])

            def blocked(_query: LogicalQuery) -> None:
                raise RuntimeError("registry changed")

            with self.assertRaisesRegex(RuntimeError, "registry changed"):
                _acquire(store, query, _ready, fetcher, before_network=blocked)
            self.assertEqual(fetcher.calls, [])
            self.assertEqual(
                store.read_verified_query_progress(query).pending_attempt_numbers,
                (),
            )

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            query = _query()

            def bug(_raw: bytes, _query: LogicalQuery) -> Mapping[str, object]:
                raise RuntimeError("parser bug")

            with self.assertRaisesRegex(RuntimeError, "parser bug"):
                _acquire(store, query, bug, ScriptedFetcher([_capture()]))
            progress = store.read_verified_query_progress(query)
            self.assertEqual(progress.status, "PENDING")
            self.assertEqual(progress.pending_attempt_numbers, (1,))
            self.assertEqual(progress.attempt_receipts, ())


class AcquisitionOrchestrationTests(unittest.TestCase):
    def _request(self, *, plan_only: bool, resume: bool = False):
        from mlb_backtest.protocols import load_run_spec
        from mlb_backtest.v52_acquire import _AcquisitionRequest

        return _AcquisitionRequest(
            root=ROOT,
            run_spec=load_run_spec(RUN_SPEC),
            years=range(2018, 2025),
            mode="hybrid",
            resume=resume,
            stop_after=None,
            legacy_root=ROOT / "data/v5_1_bt/raw",
            protected_inventory_path=(
                ROOT / "audit/v5_1_bt/protected_raw_inventory.json"
            ),
            reserve_bytes=20 * 1024**3,
            plan_only=plan_only,
        )

    def _ops(
        self,
        calls: list[str],
        *,
        phase: str | None = None,
        progress: bool = False,
    ):
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _AcquireOps,
            _RuntimeState,
        )

        def authenticate(_request):
            calls.append("authenticate")
            return _AcquisitionPlan(
                contract_sha256="a" * 64,
                query_batch_ids=("b" * 64, "c" * 64, "d" * 64),
                legacy_dispositions=(
                    "PROMOTED",
                    "FETCH_REQUIRED",
                    "FETCH_REQUIRED",
                ),
            )

        @contextmanager
        def runner_lock(_request):
            calls.append("lock")
            yield

        def inspect(_request, _plan):
            calls.append("inspect")
            return _RuntimeState(phase=phase, has_progress=progress)

        def begin(_request, _plan, _state):
            calls.append("begin")

        return _AcquireOps(
            authenticate=authenticate,
            runner_lock=runner_lock,
            inspect_runtime=inspect,
            begin_runtime=begin,
        )

    def test_plan_only_is_strictly_read_only_and_reports_exact_plan(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _run_acquisition,
        )

        calls: list[str] = []
        summary = _run_acquisition(
            self._request(plan_only=True), self._ops(calls, progress=True)
        )

        self.assertEqual(calls, ["authenticate"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            summary.pending = 1  # type: ignore[misc]
        self.assertEqual(summary.contract_sha256, "a" * 64)
        self.assertEqual(
            summary.query_batch_sha256s,
            ("b" * 64, "c" * 64, "d" * 64),
        )
        self.assertEqual(summary.promoted, 0)
        self.assertEqual(summary.fetched, 0)
        self.assertEqual(summary.planned_promoted, 1)
        self.assertEqual(summary.planned_fetch_required, 2)
        self.assertEqual(summary.resumed, 0)
        self.assertEqual(summary.quarantined, 0)
        self.assertEqual(summary.pending, 0)
        self.assertEqual(summary.network_attempts, 0)

        invalid = dataclasses.replace(
            self._ops([]),
            authenticate=lambda _request: _AcquisitionPlan(
                contract_sha256="invalid",
                query_batch_ids=("b" * 64,),
                legacy_dispositions=("FETCH_REQUIRED",),
            ),
        )
        with self.assertRaisesRegex(TrustIntegrityError, "SHA-256"):
            _run_acquisition(self._request(plan_only=True), invalid)

    def test_authentication_failure_precedes_runtime_operations(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquireOps,
            _run_acquisition,
        )

        calls: list[str] = []

        def fail(_request):
            calls.append("authenticate")
            raise TrustIntegrityError("wrong authority")

        def forbidden(*_args):
            calls.append("forbidden")
            raise AssertionError("runtime operation reached")

        ops = _AcquireOps(
            authenticate=fail,
            runner_lock=forbidden,
            inspect_runtime=forbidden,
            begin_runtime=forbidden,
        )
        with self.assertRaises(TrustIntegrityError):
            _run_acquisition(self._request(plan_only=False), ops)
        self.assertEqual(calls, ["authenticate"])

    def test_real_plan_preview_matches_closed_48_decision_authority(self) -> None:
        from mlb_backtest.protocols import load_run_spec
        from mlb_backtest.v52_acquire import acquire_sources
        from tests.test_v52_legacy_decision_inventory import (
            INVENTORY_PATH,
            LEGACY_ROOT,
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, _digest = _legacy_decision_repository(Path(directory))
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            before_directories = {
                path.relative_to(root)
                for path in root.rglob("*")
                if path.is_dir()
            }
            summary = acquire_sources(
                root,
                load_run_spec(
                    root / "protocols/v5_2_qd/historical_walkforward_500.json"
                ),
                years=range(2018, 2025),
                mode="hybrid",
                resume=False,
                stop_after=None,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=inventory_path,
                plan_only=True,
            )
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            after_directories = {
                path.relative_to(root)
                for path in root.rglob("*")
                if path.is_dir()
            }
            self.assertEqual(after, before)
            self.assertEqual(after_directories, before_directories)
            self.assertEqual(summary.promoted, 0)
            self.assertEqual(summary.fetched, 0)
            self.assertEqual(summary.planned_promoted, 1)
            self.assertEqual(summary.planned_fetch_required, 47)
            self.assertEqual(summary.pending, 0)
            self.assertEqual(summary.network_attempts, 0)
            self.assertEqual(len(summary.query_batch_sha256s), 3)
            self.assertFalse((root / INVENTORY_PATH).is_symlink())

    def test_lock_conflict_and_existing_progress_are_operational_blocks(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _run_acquisition,
        )

        calls: list[str] = []
        base = self._ops(calls)

        @contextmanager
        def conflict(_request):
            calls.append("lock")
            raise OperationalBlock("another acquisition runner is active")
            yield

        with self.assertRaisesRegex(OperationalBlock, "another"):
            _run_acquisition(
                self._request(plan_only=False),
                dataclasses.replace(base, runner_lock=conflict),
            )
        self.assertEqual(calls, ["authenticate", "lock"])

        calls.clear()
        with self.assertRaisesRegex(OperationalBlock, "--resume"):
            _run_acquisition(
                self._request(plan_only=False),
                self._ops(calls, phase="ACQUIRING", progress=True),
            )
        self.assertEqual(calls, ["authenticate", "lock", "inspect"])

    def test_real_runner_lock_rejects_symlinks_and_hardlinks(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _real_runner_lock,
        )

        lock_relative = Path(
            "audit/v5_2_qd/historical/execution/.acquisition-runner.lock"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            parent = root / lock_relative.parent
            parent.mkdir(parents=True)
            target = root / "lock-target"
            target.write_bytes(b"target")
            (root / lock_relative).symlink_to(target)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            with self.assertRaises(TrustIntegrityError):
                with _real_runner_lock(request):
                    self.fail("symlink lock was acquired")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "audit").symlink_to(outside, target_is_directory=True)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            with self.assertRaises(TrustIntegrityError):
                with _real_runner_lock(request):
                    self.fail("symlink parent was traversed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            parent = root / lock_relative.parent
            parent.mkdir(parents=True)
            peer = parent / "peer.lock"
            peer.write_bytes(b"")
            os.link(peer, root / lock_relative)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            with self.assertRaisesRegex(TrustIntegrityError, "identity"):
                with _real_runner_lock(request):
                    self.fail("hardlinked lock was acquired")

    def test_real_runner_lock_is_nonblocking_and_releases_every_fd(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _real_runner_lock,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            with _real_runner_lock(request):
                with self.assertRaisesRegex(OperationalBlock, "another"):
                    with _real_runner_lock(request):
                        self.fail("second runner acquired the lock")
            with self.assertRaisesRegex(RuntimeError, "body failed"):
                with _real_runner_lock(request):
                    raise RuntimeError("body failed")
            with _real_runner_lock(request):
                pass

    def test_real_runner_lock_detects_path_replacement_and_blocks_double_acquire(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _real_runner_lock,
        )

        lock_relative = Path(
            "audit/v5_2_qd/historical/execution/.acquisition-runner.lock"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            with self.assertRaisesRegex(TrustIntegrityError, "identity"):
                with _real_runner_lock(request):
                    lock_path = root / lock_relative
                    lock_path.unlink()
                    lock_path.write_bytes(b"replacement")
                    with self.assertRaisesRegex(OperationalBlock, "another"):
                        with _real_runner_lock(request):
                            self.fail("replacement enabled a second runner")
            with _real_runner_lock(request):
                pass
            with self.assertRaisesRegex(TrustIntegrityError, "identity"):
                with _real_runner_lock(request):
                    lock_path = root / lock_relative
                    lock_path.unlink()
                    lock_path.write_bytes(b"second replacement")
                    raise RuntimeError("body failure must not hide lock tampering")

    def test_real_runner_lock_blocks_leaf_directory_rename_recreate(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _real_runner_lock,
        )

        execution_relative = Path("audit/v5_2_qd/historical/execution")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = dataclasses.replace(self._request(plan_only=False), root=root)
            second_acquired = False
            second_blocked = False
            with self.assertRaisesRegex(TrustIntegrityError, "directory identity"):
                with _real_runner_lock(request):
                    execution = root / execution_relative
                    moved = execution.with_name("execution-moved")
                    execution.rename(moved)
                    execution.mkdir()
                    try:
                        with _real_runner_lock(request):
                            second_acquired = True
                    except OperationalBlock:
                        second_blocked = True
            self.assertFalse(second_acquired)
            self.assertTrue(second_blocked)
            with _real_runner_lock(request):
                pass

    def test_real_adapters_translate_expected_oserrors(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _AcquisitionPlan,
            _RuntimeState,
            _real_begin_runtime,
            _real_runner_lock,
        )

        with tempfile.TemporaryDirectory() as directory:
            request = dataclasses.replace(
                self._request(plan_only=False), root=Path(directory)
            )
            with (
                patch("mlb_backtest.v52_acquire.os.mkdir", side_effect=PermissionError),
                self.assertRaisesRegex(TrustIntegrityError, "directory"),
            ):
                with _real_runner_lock(request):
                    self.fail("mkdir failure was not translated")
            with (
                patch("mlb_backtest.v52_acquire.os.open", side_effect=PermissionError),
                self.assertRaisesRegex(TrustIntegrityError, "directory"),
            ):
                with _real_runner_lock(request):
                    self.fail("directory open failure was not translated")

            plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
            runtime = _RuntimeState("ACQUIRING", True)
            with (
                patch(
                    "mlb_backtest.v52_acquire.shutil.disk_usage",
                    side_effect=OSError("unavailable"),
                ),
                self.assertRaisesRegex(OperationalBlock, "free-space"),
            ):
                _real_begin_runtime(request, plan, runtime)

    def test_resume_authenticates_phase_then_stops_at_c2a_boundary(self) -> None:
        from mlb_backtest.v52_acquire import OperationalBlock, _run_acquisition

        for phase in (None, "DESIGN_ACCEPTED", "ACQUIRING"):
            calls: list[str] = []
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(OperationalBlock, "stage orchestration pending"),
            ):
                _run_acquisition(
                    self._request(plan_only=False, resume=phase is not None),
                    self._ops(
                        calls,
                        phase=phase,
                        progress=phase is not None,
                    ),
                )
            self.assertEqual(
                calls,
                ["authenticate", "lock", "inspect", "begin"],
            )

    def test_cli_exit_taxonomy_and_canonical_plan_json(self) -> None:
        from mlb_backtest.v52_acquire import (
            AcquisitionSummary,
            OperationalBlock,
            TrustIntegrityError,
            main,
        )

        argv = self._cli_argv()
        value = AcquisitionSummary(
            contract_sha256="a" * 64,
            query_batch_sha256s=("b" * 64,),
            promoted=1,
            fetched=2,
            planned_promoted=3,
            planned_fetch_required=4,
            resumed=0,
            quarantined=0,
            pending=0,
            network_attempts=0,
        )
        output = io.StringIO()
        with (
            patch("mlb_backtest.v52_acquire.acquire_sources", return_value=value),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(argv), 0)
        expected = (
            '{"contract_sha256":"'
            + "a" * 64
            + '","fetched":2,"network_attempts":0,"pending":0,'
            '"planned_fetch_required":4,"planned_promoted":3,"promoted":1,'
            '"quarantined":0,"query_batch_sha256s":["'
            + "b" * 64
            + '"],"resumed":0}\n'
        )
        self.assertEqual(output.getvalue(), expected)

        for error, code in (
            (OperationalBlock("busy"), 1),
            (TrustIntegrityError("bad trust"), 2),
        ):
            stderr = io.StringIO()
            with (
                self.subTest(error=type(error).__name__),
                patch("mlb_backtest.v52_acquire.acquire_sources", side_effect=error),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main(argv), code)
            self.assertEqual(stderr.getvalue(), f"{error}\n")
            self.assertNotIn("Traceback", stderr.getvalue())

        with (
            patch(
                "mlb_backtest.v52_acquire.acquire_sources",
                side_effect=ValueError("programmer error"),
            ),
            self.assertRaisesRegex(ValueError, "programmer error"),
        ):
            main(argv)

        with self.assertRaises(SystemExit) as misuse:
            main(["--root", str(ROOT)])
        self.assertEqual(misuse.exception.code, 2)

    def _cli_argv(self) -> list[str]:
        return [
            "--root",
            str(ROOT),
            "--run-spec",
            str(RUN_SPEC),
            "--years",
            "2018:2024",
            "--mode",
            "hybrid",
            "--legacy-root",
            str(ROOT / "data/v5_1_bt/raw"),
            "--protected-inventory",
            str(ROOT / "audit/v5_1_bt/protected_raw_inventory.json"),
            "--plan-only",
        ]

    def test_cli_parses_closed_arguments(self) -> None:
        from mlb_backtest.v52_acquire import main

        base = self._cli_argv()
        with patch(
            "mlb_backtest.v52_acquire.acquire_sources",
            side_effect=KeyboardInterrupt,
        ):
            self.assertEqual(main(base), 130)
        for extra in (
            ["--years", "2019:2024"],
            ["--mode", "fetch"],
            ["--stop-after", "static-schedules"],
            ["--unknown"],
        ):
            args = base.copy()
            option = extra[0]
            if option in args:
                index = args.index(option)
                args[index : index + 2] = extra
            else:
                args.extend(extra)
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as raised:
                main(args)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
