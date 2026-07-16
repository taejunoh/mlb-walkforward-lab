from __future__ import annotations

import dataclasses
import io
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
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
        stage_summary=None,
    ):
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
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

        def run_stages(_request, _plan):
            calls.append("stages")
            if stage_summary is None:
                raise OperationalBlock("reference stages unavailable")
            return stage_summary

        return _AcquireOps(
            authenticate=authenticate,
            runner_lock=runner_lock,
            inspect_runtime=inspect,
            begin_runtime=begin,
            run_stages=run_stages,
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

    def test_non_plan_dispatches_stage_runner_after_authenticated_runtime(self) -> None:
        from mlb_backtest.v52_acquire import AcquisitionSummary, _run_acquisition

        stage_summary = AcquisitionSummary(
            contract_sha256="a" * 64,
            query_batch_sha256s=("e" * 64, "f" * 64),
            promoted=0,
            fetched=9,
            planned_promoted=1,
            planned_fetch_required=47,
            resumed=2,
            quarantined=0,
            pending=0,
            network_attempts=9,
        )

        for phase in (None, "DESIGN_ACCEPTED", "ACQUIRING"):
            calls: list[str] = []
            with self.subTest(phase=phase):
                actual = _run_acquisition(
                    self._request(plan_only=False, resume=phase is not None),
                    self._ops(
                        calls,
                        phase=phase,
                        progress=phase is not None,
                        stage_summary=stage_summary,
                    ),
                )
            self.assertEqual(actual, stage_summary)
            self.assertEqual(
                calls,
                ["authenticate", "lock", "inspect", "begin", "stages"],
            )

    def test_pre_network_gate_reauthenticates_pending_authority_then_checks_disk(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _real_before_network,
        )

        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan(
            contract_sha256="a" * 64,
            query_batch_ids=("b" * 64, "c" * 64, "d" * 64),
            legacy_dispositions=("FETCH_REQUIRED",),
        )
        query = _query()
        query_id = logical_query_id(query)
        events: list[str] = []
        checkpoint = SimpleNamespace(
            stage="STATIC_SCHEDULES",
            query_batch_ids=("e" * 64,),
            pending_query_ids=(query_id,),
        )

        def authenticate(_request):
            events.append("authenticate")
            return plan

        def registry(_root, _run_spec):
            events.append("registry")
            return SimpleNamespace(phase="ACQUIRING")

        def latest(_root, _run_spec):
            events.append("checkpoint")
            return checkpoint

        def load_batch(_root, _batch_id):
            events.append("batch")
            return SimpleNamespace(queries=(query,))

        def runner(_request, _guard=None):
            events.append("runner")

        def disk(_root):
            events.append("disk")
            return SimpleNamespace(
                free=request.reserve_bytes + fetch_limits(query).max_bytes
            )

        with (
            patch("mlb_backtest.v52_acquire._real_authenticate", authenticate),
            patch(
                "mlb_backtest.execution_registry.load_execution_registry",
                registry,
            ),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                latest,
            ),
            patch("mlb_backtest.v52_query_plan.load_query_batch", load_batch),
            patch("mlb_backtest.v52_acquire._validate_active_runner", runner),
            patch("mlb_backtest.v52_acquire.shutil.disk_usage", disk),
        ):
            _real_before_network(
                request,
                plan,
                stage="STATIC_SCHEDULES",
                query_batch_ids=("e" * 64,),
                query=query,
            )
        self.assertEqual(
            events,
            ["authenticate", "registry", "checkpoint", "batch", "runner", "disk"],
        )

    def test_pre_network_disk_boundary_is_exact_and_failure_never_fetches(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _AcquisitionPlan,
            _real_before_network,
        )

        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        query = _query()
        checkpoint = SimpleNamespace(
            stage="STATIC_SCHEDULES",
            query_batch_ids=("e" * 64,),
            pending_query_ids=(logical_query_id(query),),
        )
        common = (
            patch("mlb_backtest.v52_acquire._real_authenticate", return_value=plan),
            patch(
                "mlb_backtest.execution_registry.load_execution_registry",
                return_value=SimpleNamespace(phase="ACQUIRING"),
            ),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                return_value=checkpoint,
            ),
            patch(
                "mlb_backtest.v52_query_plan.load_query_batch",
                return_value=SimpleNamespace(queries=(query,)),
            ),
            patch("mlb_backtest.v52_acquire._validate_active_runner"),
        )
        required = request.reserve_bytes + fetch_limits(query).max_bytes
        with (
            common[0],
            common[1],
            common[2],
            common[3],
            common[4],
            patch(
                "mlb_backtest.v52_acquire.shutil.disk_usage",
                return_value=SimpleNamespace(free=required - 1),
            ),
            self.assertRaisesRegex(OperationalBlock, "free-space"),
        ):
            _real_before_network(
                request,
                plan,
                stage="STATIC_SCHEDULES",
                query_batch_ids=("e" * 64,),
                query=query,
            )

    def test_stage_checkpoint_barriers_fetch_and_resume_without_refetch(self) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _execute_stage,
        )

        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        query = _query()
        query_id = logical_query_id(query)
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        events: list[str] = []
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            events.append("checkpoint")
            checkpoint = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}",
                **values,
            )
            checkpoints.append(checkpoint)
            return checkpoint

        def acquire(store, item, parser, **kwargs):
            kwargs["before_network"](item)
            events.append("fetch")
            capture = kwargs["fetcher"](
                item,
                timeout=kwargs["limits"].timeout,
                max_bytes=kwargs["limits"].max_bytes,
            )
            raw = capture.body
            assert raw is not None
            with store.acquire_query(item) as lease:
                return store.publish_fetched(
                    item,
                    store.next_attempt_number(lease),
                    capture,
                    parser(raw, item),
                )

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            result = _execute_stage(
                request,
                plan,
                store,
                stage="STATIC_SCHEDULES",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=None,
                fetcher=lambda *_args, **_kwargs: _capture(),
                checkpoint_publisher=publish,
                query_acquirer=acquire,
                parser=lambda _raw, _query: {},
                network_gate=lambda *_args, **_kwargs: events.append("gate"),
            )
            self.assertEqual(events, ["checkpoint", "gate", "fetch", "checkpoint"])
            self.assertEqual(result.fetched, 1)
            self.assertEqual(result.resumed, 0)
            self.assertEqual(result.network_attempts, 1)
            self.assertEqual(result.checkpoint.pending_query_ids, ())
            self.assertEqual(result.checkpoint.completed_query_ids, (query_id,))

            events.clear()
            rerun = _execute_stage(
                request,
                plan,
                store,
                stage="STATIC_SCHEDULES",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=result.checkpoint.receipt_sha256,
                fetcher=lambda *_args, **_kwargs: self.fail("verified success refetched"),
                checkpoint_publisher=publish,
                query_acquirer=lambda *_args, **_kwargs: self.fail(
                    "verified success reacquired"
                ),
                parser=lambda _raw, _query: {},
                network_gate=lambda *_args, **_kwargs: self.fail(
                    "verified success gated"
                ),
            )
            self.assertEqual(events, ["checkpoint"])
            self.assertEqual((rerun.fetched, rerun.resumed, rerun.network_attempts), (0, 1, 0))

    def test_stage_quarantine_publishes_nonterminal_checkpoint_and_blocks(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _AcquisitionPlan,
            _execute_stage,
        )

        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        published: list[Mapping[str, object]] = []

        def checkpoint(**values):
            published.append(values)
            return SimpleNamespace(receipt_sha256="f" * 64, **values)

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with store.acquire_query(query) as lease:
                store.quarantine_bodyless_attempt(
                    query,
                    store.next_attempt_number(lease),
                    _capture(None, status=None, error="PERMANENT_NETWORK"),
                    error_classification="PERMANENT_NETWORK",
                )
            with self.assertRaisesRegex(OperationalBlock, "quarantined"):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("quarantine fetched"),
                    checkpoint_publisher=checkpoint,
                    query_acquirer=lambda *_args, **_kwargs: self.fail(
                        "quarantine acquired"
                    ),
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: self.fail(
                        "quarantine gated"
                    ),
                )
        self.assertEqual(len(published), 1)
        self.assertEqual(
            published[0]["quarantined_query_ids"],
            (logical_query_id(query),),
        )

    def test_real_stage_runner_seals_static_before_inventory_and_reference(self) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _RunnerGuard,
            _StageResult,
            _real_run_stages,
        )

        request = dataclasses.replace(
            self._request(plan_only=False, resume=False),
            stop_after="reference-inputs",
        )
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        static_query = _query()
        reference_query = dataclasses.replace(
            _query(),
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        static = SimpleNamespace(batch_id="c" * 64, queries=(static_query,))
        reference = SimpleNamespace(
            batch_id="d" * 64,
            derived_from_sha256="1" * 64,
            queries=(reference_query,),
        )
        static_checkpoint = SimpleNamespace(
            receipt_sha256="e" * 64,
            stage="STATIC_SCHEDULES",
            query_batch_ids=(static.batch_id,),
            completed_query_ids=(logical_query_id(static_query),),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        reference_checkpoint = SimpleNamespace(
            receipt_sha256="f" * 64,
            stage="REFERENCE_INPUTS",
            query_batch_ids=(static.batch_id, reference.batch_id),
            completed_query_ids=tuple(
                sorted(
                    (
                        logical_query_id(static_query),
                        logical_query_id(reference_query),
                    )
                )
            ),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        inventory = SimpleNamespace(path=request.root / "schedule.json", sha256="1" * 64)
        events: list[str] = []
        runner_guard = _RunnerGuard(10, 11, request.root)

        def execute(*_args, stage, **kwargs):
            self.assertIs(kwargs["runner_guard"], runner_guard)
            events.append(f"execute:{stage}")
            if stage == "STATIC_SCHEDULES":
                return _StageResult(static_checkpoint, 7, 0, 7)
            return _StageResult(reference_checkpoint, 11, 2, 11)

        with (
            patch("mlb_backtest.v52_acquire._real_authenticate", return_value=plan),
            patch(
                "mlb_backtest.v52_acquire._current_runner_guard",
                return_value=runner_guard,
            ),
            patch(
                "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                side_effect=lambda *_args: events.append("raw-parent"),
            ),
            patch("mlb_backtest.v52_acquire.V52EvidenceStore", return_value=object()),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                side_effect=lambda *_args: events.append("load-checkpoint") or None,
            ),
            patch(
                "mlb_backtest.v52_query_plan.build_static_batch",
                side_effect=lambda *_args: events.append("build-static") or static,
            ),
            patch(
                "mlb_backtest.v52_query_plan.publish_query_batch",
                side_effect=lambda _root, batch: events.append(
                    f"publish:{'static' if batch is static else 'reference'}"
                ),
            ),
            patch(
                "mlb_backtest.v52_schedule_inventory.publish_schedule_anchor_inventory",
                side_effect=lambda *_args, **_kwargs: events.append("inventory")
                or inventory,
            ),
            patch(
                "mlb_backtest.v52_acquire._validate_schedule_inventory_schema",
                side_effect=lambda *_args: events.append("schema"),
            ),
            patch(
                "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                side_effect=lambda *_args, **_kwargs: events.append("build-reference")
                or reference,
            ),
            patch("mlb_backtest.v52_acquire._execute_stage", side_effect=execute),
        ):
            summary = _real_run_stages(request, plan)

        self.assertLess(events.index("execute:STATIC_SCHEDULES"), events.index("inventory"))
        self.assertLess(events.index("inventory"), events.index("schema"))
        self.assertLess(events.index("schema"), events.index("build-reference"))
        self.assertLess(events.index("publish:reference"), events.index("execute:REFERENCE_INPUTS"))
        self.assertEqual(summary.query_batch_sha256s, (static.batch_id, reference.batch_id))
        self.assertEqual((summary.fetched, summary.resumed, summary.network_attempts), (18, 2, 18))
        self.assertEqual((summary.pending, summary.quarantined), (0, 0))

    def test_preseason_disposition_completes_without_gate_or_network_attempt(self) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        query = LogicalQuery(
            source="mlb_stats_pitching",
            grain="season_d1_league_pitching_reference",
            endpoint="disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
            parameters={},
            requested_range={
                "as_of_date": "2024-03-19",
                "consumer_cutoff_dates": {"9001": "2024-03-19"},
                "consuming_game_pks": [9001],
                "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
                "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
                "season": 2024,
            },
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            result = SimpleNamespace(receipt_sha256=f"{len(checkpoints) + 1:064x}", **values)
            checkpoints.append(result)
            return result

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            result = _execute_stage(
                request,
                plan,
                store,
                stage="REFERENCE_INPUTS",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=None,
                fetcher=lambda *_args, **_kwargs: self.fail("derived query fetched"),
                checkpoint_publisher=publish,
                network_gate=lambda *_args, **_kwargs: self.fail("derived query gated"),
            )
            verified = store.read_verified_success(query)
        self.assertEqual(result.network_attempts, 0)
        self.assertEqual(result.fetched, 0)
        self.assertEqual(verified.published_evidence.storage_mode, "DERIVED_DISPOSITION")

    def test_stage_uses_exact_host_pools_and_publishes_only_after_workers_join(self) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        stats_queries = tuple(
            dataclasses.replace(
                _query(),
                parameters={"season": year, "sportId": 1},
                requested_range={"year": year},
            )
            for year in range(2020, 2024)
        )
        savant_queries = tuple(
            dataclasses.replace(
                _query(statcast=True, single_day=True),
                parameters={"day": day},
                requested_range={
                    "end_date": f"2024-04-0{day}",
                    "start_date": f"2024-04-0{day}",
                    "year": 2024,
                },
            )
            for day in range(1, 4)
        )
        batch = SimpleNamespace(
            batch_id="e" * 64,
            queries=(*stats_queries, *savant_queries),
        )
        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        lock = threading.Lock()
        active = {"statsapi.mlb.com": 0, "baseballsavant.mlb.com": 0}
        peak = {"statsapi.mlb.com": 0, "baseballsavant.mlb.com": 0}
        events: list[str] = []
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            with lock:
                events.append("checkpoint")
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}",
                **values,
            )
            checkpoints.append(result)
            return result

        def fetcher(query, *, timeout, max_bytes):
            del timeout, max_bytes
            host = "baseballsavant.mlb.com" if "savant" in query.endpoint else "statsapi.mlb.com"
            with lock:
                active[host] += 1
                peak[host] = max(peak[host], active[host])
            time.sleep(0.04)
            with lock:
                active[host] -= 1
                events.append(f"done:{host}")
            return dataclasses.replace(
                _capture(),
                final_url=query.endpoint,
            )

        def acquire(store, query, parser, **kwargs):
            kwargs["before_network"](query)
            capture = kwargs["fetcher"](
                query,
                timeout=kwargs["limits"].timeout,
                max_bytes=kwargs["limits"].max_bytes,
            )
            assert capture.body is not None
            with store.acquire_query(query) as lease:
                return store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    capture,
                    parser(capture.body, query),
                )

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            result = _execute_stage(
                request,
                plan,
                store,
                stage="REFERENCE_INPUTS",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=None,
                fetcher=fetcher,
                checkpoint_publisher=publish,
                query_acquirer=acquire,
                parser=lambda _raw, _query: {},
                network_gate=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(peak["statsapi.mlb.com"], 2)
        self.assertEqual(peak["baseballsavant.mlb.com"], 1)
        self.assertEqual(result.network_attempts, 7)
        self.assertEqual(result.fetched, 7)
        self.assertEqual(events.count("checkpoint"), 2)
        self.assertEqual(events[-1], "checkpoint")

    def test_stage_unexpected_runtime_error_is_not_relabelled_or_checkpointed(self) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        query = _query()
        slow_query = dataclasses.replace(
            query,
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query, slow_query))
        checkpoints: list[SimpleNamespace] = []
        slow_worker_joined = False
        rendezvous = threading.Barrier(2)

        def publish(**values):
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}",
                **values,
            )
            checkpoints.append(result)
            return result

        def parser_bug(store, item, parser, **_kwargs):
            nonlocal slow_worker_joined
            rendezvous.wait(timeout=2)
            if item == query:
                raise RuntimeError("parser bug")
            time.sleep(0.05)
            raw = b"{}"
            with store.acquire_query(item) as lease:
                result = store.publish_fetched(
                    item,
                    store.next_attempt_number(lease),
                    _capture(raw),
                    parser(raw, item),
                )
            slow_worker_joined = True
            return result

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with self.assertRaisesRegex(RuntimeError, "^parser bug$"):
                _execute_stage(
                    self._request(plan_only=False, resume=True),
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: _capture(),
                    checkpoint_publisher=publish,
                    query_acquirer=parser_bug,
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: None,
                )
        self.assertEqual(len(checkpoints), 1)
        self.assertTrue(slow_worker_joined)

    def test_fatal_parser_cancels_queued_work_before_reservation_gate_or_fetch(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        queries = tuple(
            dataclasses.replace(
                _query(),
                parameters={"season": year, "sportId": 1},
                requested_range={"year": year},
            )
            for year in range(2019, 2025)
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=queries)

        for fatal_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(fatal_type=fatal_type.__name__), tempfile.TemporaryDirectory() as directory:
                rendezvous = threading.Barrier(2)
                lock = threading.Lock()
                reservations: list[str] = []
                gates: list[str] = []
                fetches: list[str] = []
                checkpoints: list[SimpleNamespace] = []

                class TrackingBudget:
                    @contextmanager
                    def reserve(self, item):
                        with lock:
                            reservations.append(logical_query_id(item))
                        yield

                def publish(**values):
                    result = SimpleNamespace(
                        receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
                    )
                    checkpoints.append(result)
                    return result

                def parser(_raw, item):
                    if item == queries[0]:
                        raise fatal_type("fatal parser")
                    return {}

                def acquire(store, item, selected_parser, **kwargs):
                    if item in queries[:2]:
                        rendezvous.wait(timeout=2)
                    if item == queries[1]:
                        time.sleep(0.1)
                    parsed = selected_parser(b"{}", item)
                    kwargs["before_network"](item)
                    capture = kwargs["fetcher"](
                        item,
                        timeout=kwargs["limits"].timeout,
                        max_bytes=kwargs["limits"].max_bytes,
                    )
                    with store.acquire_query(item) as lease:
                        return store.publish_fetched(
                            item,
                            store.next_attempt_number(lease),
                            capture,
                            parsed,
                        )

                with self.assertRaisesRegex(fatal_type, "^fatal parser$"):
                    _execute_stage(
                        self._request(plan_only=False, resume=True),
                        _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                        V52EvidenceStore(Path(directory).resolve() / "raw"),
                        stage="STATIC_SCHEDULES",
                        batches=(batch,),
                        stage_evidence=(),
                        expected_prior=None,
                        fetcher=lambda item, **_kwargs: fetches.append(
                            logical_query_id(item)
                        )
                        or _capture(),
                        checkpoint_publisher=publish,
                        query_acquirer=acquire,
                        parser=parser,
                        network_gate=lambda _request, _plan, *, query, **_kwargs: gates.append(
                            logical_query_id(query)
                        ),
                        disk_budget=TrackingBudget(),
                    )
                self.assertEqual(len(reservations), 2)
                self.assertEqual(gates, [])
                self.assertEqual(fetches, [])
                self.assertEqual(len(checkpoints), 1)

    def test_stop_signal_is_rechecked_after_gate_immediately_before_fetch(self) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        failure = dataclasses.replace(
            _query(),
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        gated = dataclasses.replace(
            _query(),
            parameters={"season": 2024, "sportId": 1},
            requested_range={"year": 2024},
        )
        gate_entered = threading.Event()
        fatal_raised = threading.Event()
        fetches: list[str] = []
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(result)
            return result

        def parser(_raw, item):
            if item == failure:
                raise RuntimeError("fatal between gate and fetch")
            return {}

        def acquire(_store, item, selected_parser, **kwargs):
            if item == failure:
                self.assertTrue(gate_entered.wait(timeout=2))
                try:
                    selected_parser(b"{}", item)
                finally:
                    fatal_raised.set()
            kwargs["before_network"](item)
            return kwargs["fetcher"](
                item,
                timeout=kwargs["limits"].timeout,
                max_bytes=kwargs["limits"].max_bytes,
            )

        def gate(_request, _plan, *, query, **_kwargs):
            if query == gated:
                gate_entered.set()
                self.assertTrue(fatal_raised.wait(timeout=2))
                time.sleep(0.1)

        batch = SimpleNamespace(batch_id="e" * 64, queries=(failure, gated))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "^fatal between gate and fetch$"):
                _execute_stage(
                    self._request(plan_only=False, resume=True),
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    V52EvidenceStore(Path(directory).resolve() / "raw"),
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda item, **_kwargs: fetches.append(
                        logical_query_id(item)
                    )
                    or _capture(),
                    checkpoint_publisher=publish,
                    query_acquirer=acquire,
                    parser=parser,
                    network_gate=gate,
                )
        self.assertEqual(fetches, [])
        self.assertEqual(len(checkpoints), 1)

    def test_mixed_worker_failures_use_deterministic_precedence(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _AcquisitionPlan,
            _execute_stage,
        )

        programmer = dataclasses.replace(
            _query(),
            parameters={"season": 2020, "sportId": 1},
            requested_range={"year": 2020},
        )
        trust = dataclasses.replace(
            _query(),
            parameters={"season": 2021, "sportId": 1},
            requested_range={"year": 2021},
        )
        operational = _query(statcast=True, single_day=True)
        batch = SimpleNamespace(
            batch_id="e" * 64,
            queries=(programmer, trust, operational),
        )
        selected: list[str] = []

        for delays in ((0.1, 0.05, 0.0), (0.0, 0.05, 0.1)):
            with self.subTest(delays=delays), tempfile.TemporaryDirectory() as directory:
                rendezvous = threading.Barrier(3)
                delay_by_id = {
                    logical_query_id(query): delay
                    for query, delay in zip(batch.queries, delays, strict=True)
                }
                checkpoints: list[SimpleNamespace] = []

                def publish(**values):
                    result = SimpleNamespace(
                        receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
                    )
                    checkpoints.append(result)
                    return result

                def fail(_store, item, _parser, **_kwargs):
                    rendezvous.wait(timeout=2)
                    time.sleep(delay_by_id[logical_query_id(item)])
                    if item == programmer:
                        raise RuntimeError(f"programmer:{logical_query_id(item)}")
                    if item == trust:
                        raise TrustIntegrityError(f"trust:{logical_query_id(item)}")
                    raise OperationalBlock(f"operational:{logical_query_id(item)}")

                with self.assertRaises(RuntimeError) as raised:
                    _execute_stage(
                        self._request(plan_only=False, resume=True),
                        _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                        V52EvidenceStore(Path(directory).resolve() / "raw"),
                        stage="REFERENCE_INPUTS",
                        batches=(batch,),
                        stage_evidence=(),
                        expected_prior=None,
                        fetcher=lambda *_args, **_kwargs: self.fail("mixed fetched"),
                        checkpoint_publisher=publish,
                        query_acquirer=fail,
                        parser=lambda _raw, _query: {},
                        network_gate=lambda *_args, **_kwargs: None,
                    )
                selected.append(str(raised.exception))
                self.assertEqual(len(checkpoints), 1)
        expected = f"programmer:{logical_query_id(programmer)}"
        self.assertEqual(selected, [expected, expected])

    def test_stage_failure_selector_freezes_full_taxonomy_and_tie_break(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            SplitRequired,
            TerminalAcquisitionFailure,
            TrustIntegrityError,
            _StageFailure,
            _select_stage_failure,
        )

        ordered_errors = (
            KeyboardInterrupt("interrupt"),
            RuntimeError("programmer"),
            TrustIntegrityError("trust"),
            SplitRequired("TIMEOUT", 6),
            OperationalBlock("operational"),
            TerminalAcquisitionFailure("PERMANENT_NETWORK", 6),
        )
        failures = tuple(
            _StageFailure(f"{index:064x}", index, error)
            for index, error in enumerate(ordered_errors)
        )
        for expected_index in range(len(failures)):
            selected = _select_stage_failure(tuple(reversed(failures[expected_index:])))
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertIs(selected.error, ordered_errors[expected_index])

        canonical = RuntimeError("canonical")
        later = RuntimeError("later")
        selected = _select_stage_failure(
            (
                _StageFailure("b" * 64, 0, later),
                _StageFailure("a" * 64, 9, canonical),
            )
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertIs(selected.error, canonical)

    def test_coordinator_interrupt_cancels_every_host_pool_before_join(self) -> None:
        import concurrent.futures

        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        savant = _query(statcast=True, single_day=True)
        stats = tuple(
            dataclasses.replace(
                _query(),
                parameters={"season": year, "sportId": 1},
                requested_range={"year": year},
            )
            for year in (2023, 2024)
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(savant, *stats))

        for interrupt_site in ("submission", "as_completed"):
            with self.subTest(site=interrupt_site), tempfile.TemporaryDirectory() as directory:
                gates: list[str] = []
                fetches: list[str] = []
                checkpoints: list[SimpleNamespace] = []

                def publish(**values):
                    result = SimpleNamespace(
                        receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
                    )
                    checkpoints.append(result)
                    return result

                def acquire(_store, item, _parser, **kwargs):
                    time.sleep(0.1)
                    kwargs["before_network"](item)
                    return kwargs["fetcher"](
                        item,
                        timeout=kwargs["limits"].timeout,
                        max_bytes=kwargs["limits"].max_bytes,
                    )

                original_submit = concurrent.futures.ThreadPoolExecutor.submit
                submit_count = 0

                def interrupting_submit(executor, *args, **kwargs):
                    nonlocal submit_count
                    submit_count += 1
                    if interrupt_site == "submission" and submit_count == 2:
                        raise KeyboardInterrupt("coordinator submission interrupt")
                    return original_submit(executor, *args, **kwargs)

                completed_patch = (
                    patch(
                        "mlb_backtest.v52_acquire.concurrent.futures.as_completed",
                        side_effect=KeyboardInterrupt(
                            "coordinator completion interrupt"
                        ),
                    )
                    if interrupt_site == "as_completed"
                    else nullcontext()
                )
                expected = (
                    "^coordinator submission interrupt$"
                    if interrupt_site == "submission"
                    else "^coordinator completion interrupt$"
                )
                with (
                    patch.object(
                        concurrent.futures.ThreadPoolExecutor,
                        "submit",
                        interrupting_submit,
                    ),
                    completed_patch,
                    self.assertRaisesRegex(KeyboardInterrupt, expected),
                ):
                    _execute_stage(
                        self._request(plan_only=False, resume=True),
                        _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                        V52EvidenceStore(Path(directory).resolve() / "raw"),
                        stage="REFERENCE_INPUTS",
                        batches=(batch,),
                        stage_evidence=(),
                        expected_prior=None,
                        fetcher=lambda item, **_kwargs: fetches.append(
                            logical_query_id(item)
                        )
                        or _capture(),
                        checkpoint_publisher=publish,
                        query_acquirer=acquire,
                        parser=lambda _raw, _query: {},
                        network_gate=lambda _request, _plan, *, query, **_kwargs: gates.append(
                            logical_query_id(query)
                        ),
                    )
                self.assertEqual(gates, [])
                self.assertEqual(fetches, [])
                self.assertEqual(len(checkpoints), 1)

    def test_retry_sleep_is_stop_aware_and_releases_reservation_promptly(self) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _DiskBudget,
            _execute_stage,
        )

        sleeping = dataclasses.replace(
            _query(),
            parameters={"season": 2023, "sportId": 1},
            requested_range={"year": 2023},
        )
        fatal = dataclasses.replace(
            _query(),
            parameters={"season": 2024, "sportId": 1},
            requested_range={"year": 2024},
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(sleeping, fatal))
        request = self._request(plan_only=False, resume=True)
        budget = _DiskBudget(
            request,
            free_bytes=lambda _root: request.reserve_bytes
            + 2 * fetch_limits(sleeping).max_bytes,
        )
        sleeper_entered = threading.Event()
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(result)
            return result

        def acquire(_store, item, _parser, **kwargs):
            if item == sleeping:
                sleeper_entered.set()
                kwargs["sleeper"](3600)
                self.fail("stop-aware sleeper returned")
            self.assertTrue(sleeper_entered.wait(timeout=2))
            time.sleep(0.05)
            raise RuntimeError("fatal wakes retry sleeper")

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "^fatal wakes retry sleeper$"):
                _execute_stage(
                    request,
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    V52EvidenceStore(Path(directory).resolve() / "raw"),
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("retry fetched"),
                    checkpoint_publisher=publish,
                    query_acquirer=acquire,
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: None,
                    disk_budget=budget,
                )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(budget.reserved_bytes, 0)
        self.assertEqual(len(checkpoints), 1)

    def test_same_host_domain_failures_admit_queued_higher_precedence_error(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _AcquisitionPlan,
            _execute_stage,
        )

        queries = tuple(
            dataclasses.replace(
                _query(),
                parameters={"order": order, "season": 2024, "sportId": 1},
                requested_range={"order": order, "year": 2024},
            )
            for order in (1, 2, 3, 4)
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=queries)
        selected: list[str] = []

        for trust_delay, programmer_delay in ((0.05, 0.0), (0.0, 0.05)):
            with self.subTest(
                trust_delay=trust_delay,
                programmer_delay=programmer_delay,
            ), tempfile.TemporaryDirectory() as directory:
                admitted: list[str] = []
                operational_completed = threading.Event()
                trust_admitted = threading.Event()
                queued_rendezvous = threading.Barrier(2)
                checkpoints: list[SimpleNamespace] = []

                def publish(**values):
                    result = SimpleNamespace(
                        receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
                    )
                    checkpoints.append(result)
                    return result

                def fail(_store, item, _parser, **_kwargs):
                    admitted.append(logical_query_id(item))
                    if item == queries[0]:
                        operational_completed.set()
                        raise OperationalBlock("lower operational")
                    if item == queries[1]:
                        self.assertTrue(trust_admitted.wait(timeout=2))
                        raise OperationalBlock("capacity blocker")
                    self.assertTrue(operational_completed.is_set())
                    if item == queries[2]:
                        trust_admitted.set()
                        queued_rendezvous.wait(timeout=2)
                        time.sleep(trust_delay)
                        raise TrustIntegrityError("queued trust")
                    queued_rendezvous.wait(timeout=2)
                    time.sleep(programmer_delay)
                    raise RuntimeError(f"queued programmer:{logical_query_id(item)}")

                with self.assertRaises(RuntimeError) as raised:
                    _execute_stage(
                        self._request(plan_only=False, resume=True),
                        _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                        V52EvidenceStore(Path(directory).resolve() / "raw"),
                        stage="REFERENCE_INPUTS",
                        batches=(batch,),
                        stage_evidence=(),
                        expected_prior=None,
                        fetcher=lambda *_args, **_kwargs: self.fail("domain fetched"),
                        checkpoint_publisher=publish,
                        query_acquirer=fail,
                        parser=lambda _raw, _query: {},
                        network_gate=lambda *_args, **_kwargs: None,
                    )
                selected.append(str(raised.exception))
                self.assertEqual(
                    sorted(admitted),
                    sorted(logical_query_id(query) for query in queries),
                )
                self.assertEqual(len(checkpoints), 1)
        expected = f"queued programmer:{logical_query_id(queries[3])}"
        self.assertEqual(selected, [expected, expected])

    def test_stage_rejects_unknown_network_host_before_checkpoint(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _execute_stage,
        )

        query = dataclasses.replace(_query(), endpoint="https://example.com/data")
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        published = False

        def publish(**_values):
            nonlocal published
            published = True
            self.fail("unknown host checkpointed")

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with self.assertRaisesRegex(TrustIntegrityError, "host"):
                _execute_stage(
                    self._request(plan_only=False, resume=True),
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("unknown host fetched"),
                    checkpoint_publisher=publish,
                )
        self.assertFalse(published)

    def test_real_stage_integrity_failures_translate_to_trust_errors(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _RunnerGuard,
            _real_run_stages,
        )

        request = dataclasses.replace(
            self._request(plan_only=False, resume=True),
            stop_after="reference-inputs",
        )
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        guard = _RunnerGuard(10, 11, request.root)
        with (
            patch("mlb_backtest.v52_acquire._real_authenticate", return_value=plan),
            patch("mlb_backtest.v52_acquire._current_runner_guard", return_value=guard),
            patch(
                "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                return_value=request.root / "data/v5_2_qd/historical/raw",
            ),
            patch("mlb_backtest.v52_acquire.V52EvidenceStore", return_value=object()),
            patch(
                "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                return_value=None,
            ),
            patch(
                "mlb_backtest.v52_query_plan.build_static_batch",
                side_effect=ValueError("forged static batch"),
            ),
            self.assertRaisesRegex(TrustIntegrityError, "static"),
        ):
            _real_run_stages(request, plan)

    def test_real_checkpoint_integrity_failure_translates_but_injected_bug_does_not(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _execute_stage,
        )

        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with (
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=ValueError("checkpoint forged"),
                ),
                self.assertRaisesRegex(TrustIntegrityError, "checkpoint"),
            ):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: _capture(),
                )

            def injected_bug(**_values):
                raise RuntimeError("injected checkpoint bug")

            with self.assertRaisesRegex(RuntimeError, "^injected checkpoint bug$"):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: _capture(),
                    checkpoint_publisher=injected_bug,
                )

    def test_stage_terminal_and_split_outcomes_keep_exact_taxonomy(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            SplitRequired,
            TerminalAcquisitionFailure,
            TrustIntegrityError,
            _AcquisitionPlan,
            _execute_stage,
        )

        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "terminal")
            checkpoints: list[SimpleNamespace] = []

            def publish(**values):
                result = SimpleNamespace(
                    receipt_sha256=f"{len(checkpoints) + 1:064x}",
                    **values,
                )
                checkpoints.append(result)
                return result

            def terminal(store, item, _parser, **_kwargs):
                with store.acquire_query(item) as lease:
                    attempt = store.next_attempt_number(lease)
                    store.quarantine_bodyless_attempt(
                        item,
                        attempt,
                        _capture(None, status=None, error="PERMANENT_NETWORK"),
                        error_classification="PERMANENT_NETWORK",
                    )
                raise TerminalAcquisitionFailure("PERMANENT_NETWORK", attempt)

            with self.assertRaisesRegex(OperationalBlock, "quarantined"):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("terminal fetched"),
                    checkpoint_publisher=publish,
                    query_acquirer=terminal,
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: None,
                )
            self.assertEqual(len(checkpoints), 2)
            self.assertEqual(
                checkpoints[-1].quarantined_query_ids,
                (logical_query_id(query),),
            )

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "split")
            checkpoints = []

            def split(*_args, **_kwargs):
                raise SplitRequired("TIMEOUT", 6)

            with self.assertRaisesRegex(TrustIntegrityError, "cannot split"):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("split fetched"),
                    checkpoint_publisher=publish,
                    query_acquirer=split,
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: None,
                )
            self.assertEqual(len(checkpoints), 1)

    def test_success_before_checkpoint_recovery_never_rewrites_evidence_or_refetches(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            raw = b"{}"
            with store.acquire_query(query) as lease:
                store.publish_fetched(
                    query,
                    store.next_attempt_number(lease),
                    _capture(raw),
                    {},
                )
            before = {
                path.relative_to(store.root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in store.root.rglob("*")
                if path.is_file()
            }
            checkpoints: list[SimpleNamespace] = []

            def publish(**values):
                result = SimpleNamespace(receipt_sha256="f" * 64, **values)
                checkpoints.append(result)
                return result

            result = _execute_stage(
                self._request(plan_only=False, resume=True),
                _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                store,
                stage="STATIC_SCHEDULES",
                batches=(batch,),
                stage_evidence=(),
                expected_prior="d" * 64,
                fetcher=lambda *_args, **_kwargs: self.fail("success refetched"),
                checkpoint_publisher=publish,
                query_acquirer=lambda *_args, **_kwargs: self.fail(
                    "success reacquired"
                ),
                parser=lambda _raw, _query: {},
                network_gate=lambda *_args, **_kwargs: self.fail("success gated"),
            )
            after = {
                path.relative_to(store.root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in store.root.rglob("*")
                if path.is_file()
            }
        self.assertEqual(after, before)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual((result.fetched, result.resumed, result.network_attempts), (0, 1, 0))

    def test_raw_parent_bootstrap_rejects_nofollow_ancestor_attack(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _ensure_canonical_raw_parent,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "repo"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "data").symlink_to(outside, target_is_directory=True)
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            with self.assertRaisesRegex(TrustIntegrityError, "raw evidence parent"):
                _ensure_canonical_raw_parent(request)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_stop_after_leaves_registry_acquiring_and_creates_no_timezone_outputs(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            AcquisitionSummary,
            _AcquisitionPlan,
            _AcquireOps,
            _RuntimeState,
            _run_acquisition,
        )

        state = {"phase": "DESIGN_ACCEPTED"}
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        summary = AcquisitionSummary(
            contract_sha256="a" * 64,
            query_batch_sha256s=("c" * 64, "d" * 64),
            promoted=0,
            fetched=2,
            planned_promoted=0,
            planned_fetch_required=1,
            resumed=0,
            quarantined=0,
            pending=0,
            network_attempts=2,
        )

        @contextmanager
        def lock(_request):
            yield

        def begin(_request, _plan, _runtime):
            state["phase"] = "ACQUIRING"

        def stages(request, _plan):
            self.assertEqual(request.stop_after, "reference-inputs")
            self.assertEqual(state["phase"], "ACQUIRING")
            return summary

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = dataclasses.replace(
                self._request(plan_only=False, resume=False),
                root=root,
                stop_after="reference-inputs",
            )
            actual = _run_acquisition(
                request,
                _AcquireOps(
                    authenticate=lambda _request: plan,
                    runner_lock=lock,
                    inspect_runtime=lambda _request, _plan: _RuntimeState(
                        "DESIGN_ACCEPTED", False
                    ),
                    begin_runtime=begin,
                    run_stages=stages,
                ),
            )
            timezone_root = root / "audit/v5_2_qd/historical/acquisition/timezone_reviews"
            self.assertFalse(timezone_root.exists())
        self.assertEqual(actual, summary)
        self.assertEqual(state["phase"], "ACQUIRING")

    def test_shared_disk_budget_prevents_cross_pool_overcommit_and_releases(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _AcquisitionPlan,
            _DiskBudget,
            _execute_stage,
        )

        queries = tuple(
            dataclasses.replace(
                _query(),
                parameters={"season": year, "sportId": 1},
                requested_range={"year": year},
            )
            for year in range(2020, 2023)
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=queries)
        request = self._request(plan_only=False, resume=True)
        max_bytes = fetch_limits(queries[0]).max_bytes
        budget = _DiskBudget(
            request,
            free_bytes=lambda _root: request.reserve_bytes + max_bytes,
        )
        fetch_calls = 0
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}",
                **values,
            )
            checkpoints.append(result)
            return result

        def acquire(store, item, parser, **kwargs):
            nonlocal fetch_calls
            kwargs["before_network"](item)
            fetch_calls += 1
            time.sleep(0.08)
            capture = kwargs["fetcher"](
                item,
                timeout=kwargs["limits"].timeout,
                max_bytes=kwargs["limits"].max_bytes,
            )
            assert capture.body is not None
            with store.acquire_query(item) as lease:
                return store.publish_fetched(
                    item,
                    store.next_attempt_number(lease),
                    capture,
                    parser(capture.body, item),
                )

        def gate(*_args, disk_budget, **_kwargs):
            self.assertIs(disk_budget, budget)
            disk_budget.recheck()

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with self.assertRaisesRegex(OperationalBlock, "free-space"):
                _execute_stage(
                    request,
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda query, **_kwargs: dataclasses.replace(
                        _capture(), final_url=query.endpoint
                    ),
                    checkpoint_publisher=publish,
                    query_acquirer=acquire,
                    parser=lambda _raw, _query: {},
                    network_gate=gate,
                    disk_budget=budget,
                )
        self.assertEqual(fetch_calls, 1)
        self.assertEqual(budget.peak_reserved_bytes, max_bytes)
        self.assertEqual(budget.reserved_bytes, 0)
        self.assertEqual(len(checkpoints), 1)

    def test_disk_budget_stays_reserved_across_retries_and_releases_on_errors(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            SplitRequired,
            TerminalAcquisitionFailure,
            TrustIntegrityError,
            _AcquisitionPlan,
            _DiskBudget,
            _execute_stage,
        )

        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        max_bytes = fetch_limits(query).max_bytes

        error_cases = (
            (SplitRequired("TIMEOUT", 6), TrustIntegrityError),
            (TerminalAcquisitionFailure("PERMANENT_NETWORK", 6), OperationalBlock),
            (RuntimeError("worker bug"), RuntimeError),
        )
        for error, expected in error_cases:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                budget = _DiskBudget(
                    request,
                    free_bytes=lambda _root: request.reserve_bytes + max_bytes,
                )
                store = V52EvidenceStore(Path(directory).resolve() / "raw")
                checkpoints: list[SimpleNamespace] = []

                def publish(**values):
                    result = SimpleNamespace(
                        receipt_sha256=f"{len(checkpoints) + 1:064x}",
                        **values,
                    )
                    checkpoints.append(result)
                    return result

                def fail(*_args, **_kwargs):
                    self.assertEqual(budget.reserved_bytes, max_bytes)
                    raise error

                with self.assertRaises(expected):
                    _execute_stage(
                        request,
                        _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                        store,
                        stage="STATIC_SCHEDULES",
                        batches=(batch,),
                        stage_evidence=(),
                        expected_prior=None,
                        fetcher=lambda *_args, **_kwargs: self.fail("error fetched"),
                        checkpoint_publisher=publish,
                        query_acquirer=fail,
                        parser=lambda _raw, _query: {},
                        network_gate=lambda *_args, **_kwargs: None,
                        disk_budget=budget,
                    )
                self.assertEqual(budget.reserved_bytes, 0)

        with tempfile.TemporaryDirectory() as directory:
            budget = _DiskBudget(
                request,
                free_bytes=lambda _root: request.reserve_bytes + max_bytes,
            )
            store = V52EvidenceStore(Path(directory).resolve() / "retry")
            checkpoints = []

            def publish_retry(**values):
                result = SimpleNamespace(
                    receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
                )
                checkpoints.append(result)
                return result

            def retry(store, item, parser, **kwargs):
                for _ in range(2):
                    self.assertEqual(budget.reserved_bytes, max_bytes)
                    kwargs["before_network"](item)
                raw = b"{}"
                with store.acquire_query(item) as lease:
                    return store.publish_fetched(
                        item,
                        store.next_attempt_number(lease),
                        _capture(raw),
                        parser(raw, item),
                    )

            def retry_gate(*_args, disk_budget, **_kwargs):
                self.assertIs(disk_budget, budget)
                disk_budget.recheck()

            result = _execute_stage(
                request,
                _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                store,
                stage="STATIC_SCHEDULES",
                batches=(batch,),
                stage_evidence=(),
                expected_prior=None,
                fetcher=lambda *_args, **_kwargs: self.fail("retry fake fetched"),
                checkpoint_publisher=publish_retry,
                query_acquirer=retry,
                parser=lambda _raw, _query: {},
                network_gate=retry_gate,
                disk_budget=budget,
            )
        self.assertEqual(result.network_attempts, 0)
        self.assertEqual(budget.reserved_bytes, 0)

    def test_exhausted_dangling_reservations_checkpoint_and_report_pending(self) -> None:
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            _AcquisitionPlan,
            _DiskBudget,
            _execute_stage,
        )

        query = _query()
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        max_bytes = fetch_limits(query).max_bytes
        budget = _DiskBudget(
            request,
            free_bytes=lambda _root: request.reserve_bytes + max_bytes,
        )
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(result)
            return result

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with store.acquire_query(query) as lease:
                self.assertEqual(
                    tuple(store.next_attempt_number(lease) for _ in range(6)),
                    (1, 2, 3, 4, 5, 6),
                )
            with self.assertRaisesRegex(
                OperationalBlock,
                "^STATIC_SCHEDULES contains pending query evidence$",
            ):
                _execute_stage(
                    request,
                    _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",)),
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail("exhausted fetched"),
                    checkpoint_publisher=publish,
                    parser=lambda _raw, _query: {},
                    network_gate=lambda *_args, **_kwargs: self.fail(
                        "exhausted gated"
                    ),
                    disk_budget=budget,
                )
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[-1].pending_query_ids, (logical_query_id(query),))
        self.assertEqual(checkpoints[-1].quarantined_query_ids, ())
        self.assertEqual(budget.reserved_bytes, 0)

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
