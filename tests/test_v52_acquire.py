from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, patch

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

    def test_schedule_inventory_and_timezone_paths_follow_exact_run_authority(
        self,
    ) -> None:
        import shutil

        from mlb_backtest.execution_registry import publish_acquisition_checkpoint
        from mlb_backtest.protocols import load_run_spec, load_source_run_spec
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _load_required_timezone_gate,
            _schedule_inventory_relative,
            _timezone_stage_evidence_relatives,
            _validate_schedule_inventory_schema,
        )
        from mlb_backtest.v52_query_plan import build_static_batch
        from mlb_backtest.v52_schedule_inventory import (
            publish_schedule_anchor_inventory,
        )
        from tests.test_v52_schedule_inventory import (
            ROOT as INVENTORY_ROOT,
            SOURCE_RUN_SPEC_PATH,
            _memoized_disposition_projection_patch,
            _memoized_registry_git_patch,
            _source_registry_fixture,
        )

        with (
            _memoized_registry_git_patch(),
            _memoized_disposition_projection_patch(),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = _source_registry_fixture(Path(directory))
            static = build_static_batch(
                fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(INVENTORY_ROOT),
                fixture.contract_path,
            )
            disposition_query_id = logical_query_id(static.queries[1])
            publish_acquisition_checkpoint(
                fixture.root,
                fixture.run_spec,
                expected_prior_checkpoint_sha256=None,
                stage="STATIC_SCHEDULES",
                query_batch_ids=(static.batch_id,),
                completed_query_ids=tuple(
                    sorted(logical_query_id(query) for query in static.queries)
                ),
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                dispositioned_query_ids=(disposition_query_id,),
            )
            verified = publish_schedule_anchor_inventory(
                fixture.root,
                fixture.run_spec,
                acquisition_contract_path=fixture.contract_path,
                static_batch_id=static.batch_id,
                evidence_root=fixture.evidence_root,
            )
            _validate_schedule_inventory_schema(
                fixture.root, fixture.run_spec, verified
            )

            import mlb_backtest.v52_acquire as acquire

            request = acquire._AcquisitionRequest(
                root=fixture.root,
                run_spec=fixture.run_spec,
                years=range(2018, 2025),
                mode="hybrid",
                resume=True,
                stop_after="reference-inputs",
                legacy_root=fixture.root / "data/v5_1_bt/raw",
                protected_inventory_path=(
                    fixture.root / "audit/v5_1_bt/protected_raw_inventory.json"
                ),
                reserve_bytes=20 * 1024**3,
                plan_only=False,
                fetcher=lambda *_args, **_kwargs: self.fail(
                    "source STATIC-to-REFERENCE resume fetched"
                ),
                acquisition_contract_path=fixture.contract_path,
            )
            plan = acquire._AcquisitionPlan(
                contract_sha256=hashlib.sha256(
                    fixture.contract_path.read_bytes()
                ).hexdigest(),
                query_batch_ids=(static.batch_id,),
                legacy_dispositions=("PROMOTED",) + ("FETCH_REQUIRED",) * 47,
            )

            def finish_stage(*_args, stage, batches, **_kwargs):
                query_ids = tuple(
                    sorted(
                        logical_query_id(query)
                        for batch in batches
                        for query in batch.queries
                    )
                )
                checkpoint = SimpleNamespace(
                    receipt_sha256=("e" if stage == "STATIC_SCHEDULES" else "f") * 64,
                    stage=stage,
                    query_batch_ids=tuple(batch.batch_id for batch in batches),
                    completed_query_ids=query_ids,
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )
                return acquire._StageResult(
                    checkpoint=checkpoint,
                    fetched=0,
                    resumed=len(query_ids),
                    network_attempts=0,
                )

            with (
                patch.object(acquire, "_real_authenticate", return_value=plan),
                patch.object(
                    acquire,
                    "_current_runner_guard",
                    return_value=acquire._RunnerGuard(-1, -1, fixture.root),
                ),
                patch.object(acquire, "_execute_stage", side_effect=finish_stage),
                patch.object(
                    acquire,
                    "_validate_schedule_inventory_schema",
                    wraps=_validate_schedule_inventory_schema,
                ) as schema_validation,
            ):
                summary = acquire._real_run_stages(request, plan)
            schema_validation.assert_called_once_with(
                fixture.root, fixture.run_spec, ANY
            )
            self.assertEqual(summary.query_batch_sha256s[0], static.batch_id)
            self.assertEqual(summary.network_attempts, 0)

            source_relative = Path(
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "schedule_anchor_inventory.json"
            )
            old_relative = Path(
                "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
            )
            self.assertEqual(
                _schedule_inventory_relative(fixture.run_spec), source_relative
            )
            source_timezone = _timezone_stage_evidence_relatives(fixture.run_spec)
            self.assertEqual(source_timezone[0], source_relative)
            self.assertTrue(
                all(
                    "historical_superseding_565534" in relative.as_posix()
                    for relative in source_timezone[:-1]
                )
            )

            old_run = load_run_spec(
                fixture.root / "protocols/v5_2_qd/historical_walkforward_500.json"
            )
            self.assertEqual(_schedule_inventory_relative(old_run), old_relative)
            self.assertEqual(
                _timezone_stage_evidence_relatives(old_run)[0], old_relative
            )
            for relative in (
                _timezone_stage_evidence_relatives(old_run)[index]
                for index in (1, 2, 4, 5)
            ):
                path = fixture.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("{}\n")
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError(
                        "source timezone gate fell back to old evidence"
                    ),
                ),
                self.assertRaises(OperationalBlock),
            ):
                _load_required_timezone_gate(fixture.root, fixture.run_spec)

            old_path = fixture.root / old_relative
            old_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(verified.path, old_path)
            verified.path.unlink()
            with self.assertRaises(TrustIntegrityError):
                _validate_schedule_inventory_schema(
                    fixture.root, fixture.run_spec, verified
                )

            source_run = load_source_run_spec(
                fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(INVENTORY_ROOT)
            )
            self.assertEqual(_schedule_inventory_relative(source_run), source_relative)

    def test_source_timezone_continuation_uses_exact_authority_and_evidence(
        self,
    ) -> None:
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _RunnerGuard,
            _real_run_stages,
            _timezone_stage_evidence_relatives,
        )

        source_run = load_source_run_spec(
            ROOT / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract = Path(
            "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
        )
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
        completed_ids = tuple(
            sorted((logical_query_id(static_query), logical_query_id(reference_query)))
        )
        dispositioned_id = logical_query_id(static_query)
        latest = SimpleNamespace(
            receipt_sha256="e" * 64,
            stage="REFERENCE_INPUTS",
            query_batch_ids=(static.batch_id, reference.batch_id),
            completed_query_ids=completed_ids,
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        plan = _AcquisitionPlan(
            "a" * 64,
            (static.batch_id,),
            ("PROMOTED",) + ("FETCH_REQUIRED",) * 47,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
                run_spec=source_run,
                acquisition_contract_path=root / source_contract,
            )
            inventory = SimpleNamespace(
                path=root / "audit/v5_2_qd/historical_superseding_565534/"
                "acquisition/schedule_anchor_inventory.json",
                sha256="1" * 64,
            )
            evidence = tuple(
                root / relative
                for relative in _timezone_stage_evidence_relatives(source_run)
            )
            for path in evidence:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
            checkpoint_calls: list[Mapping[str, object]] = []

            def publish_checkpoint(*_args, **kwargs):
                checkpoint_calls.append(kwargs)
                return SimpleNamespace(
                    receipt_sha256="f" * 64,
                    stage="TIMEZONE_REVIEW",
                    query_batch_ids=tuple(kwargs["query_batch_ids"]),
                    completed_query_ids=completed_ids,
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                    split_receipt_sha256s=(),
                    stage_evidence_receipt_paths=tuple(
                        path.relative_to(root).as_posix() for path in evidence
                    ),
                    stage_evidence_receipt_sha256s=tuple(
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in evidence
                    ),
                )

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._real_authenticate",
                        return_value=plan,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._current_runner_guard",
                        return_value=_RunnerGuard(-1, -1, root),
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                        return_value=root
                        / "data/v5_2_qd/historical_superseding_565534/raw",
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire.V52EvidenceStore",
                        return_value=object(),
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.execution_registry."
                        "load_latest_acquisition_checkpoint",
                        return_value=latest,
                    )
                )
                publish = stack.enter_context(
                    patch(
                        "mlb_backtest.execution_registry."
                        "publish_acquisition_checkpoint",
                        side_effect=publish_checkpoint,
                    )
                )
                scope = stack.enter_context(
                    patch(
                        "mlb_backtest.execution_registry."
                        "_using_verified_source_checkpoint",
                        return_value=nullcontext(),
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.build_static_batch",
                        return_value=static,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.load_query_batch_for_authority",
                        side_effect=lambda _root, batch_id, _authority: {
                            static.batch_id: static,
                            reference.batch_id: reference,
                        }[batch_id],
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_schedule_inventory."
                        "load_verified_schedule_anchor_inventory",
                        return_value=inventory,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._validate_schedule_inventory_schema"
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                        return_value=reference,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._stage_populations",
                        return_value=(completed_ids, (), ()),
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._source_disposition_authorities",
                        return_value={dispositioned_id: object()},
                    )
                )
                candidate = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_timezone_review.publish_timezone_candidate",
                        return_value=evidence[3],
                    )
                )
                gate = stack.enter_context(
                    patch("mlb_backtest.v52_acquire._load_required_timezone_gate")
                )
                game_inventory = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_game_evidence_inventory."
                        "publish_game_evidence_inventory",
                        side_effect=RuntimeError("after source timezone checkpoint"),
                    )
                )
                with self.assertRaisesRegex(TrustIntegrityError, "game evidence"):
                    _real_run_stages(request, plan)

            candidate.assert_called_once_with(
                root=root,
                evidence_root=(root / "data/v5_2_qd/historical_superseding_565534/raw"),
                batch_ids=(static.batch_id, reference.batch_id),
                run_spec_path=(
                    root / "protocols/v5_2_qd/"
                    "historical_walkforward_500_superseding_565534.json"
                ),
                acquisition_contract_path=root / source_contract,
            )
            gate.assert_called_once_with(root, source_run)
            scope.assert_called_once_with(root, source_run)
            game_inventory.assert_called_once_with(
                root,
                run_spec_path=(
                    root / "protocols/v5_2_qd/"
                    "historical_walkforward_500_superseding_565534.json"
                ),
                acquisition_contract_path=root / source_contract,
            )
            publish.assert_called_once()
            self.assertEqual(len(checkpoint_calls), 1)
            self.assertEqual(checkpoint_calls[0]["stage"], "TIMEZONE_REVIEW")
            self.assertEqual(checkpoint_calls[0]["stage_evidence_receipts"], evidence)
            self.assertEqual(
                checkpoint_calls[0]["dispositioned_query_ids"],
                (dispositioned_id,),
            )

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
                path.relative_to(root) for path in root.rglob("*") if path.is_dir()
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
                path.relative_to(root) for path in root.rglob("*") if path.is_dir()
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
        self.assertEqual(calls, ["authenticate", "inspect", "lock"])

        calls.clear()
        with self.assertRaisesRegex(OperationalBlock, "--resume"):
            _run_acquisition(
                self._request(plan_only=False),
                self._ops(calls, phase="ACQUIRING", progress=True),
            )
        self.assertEqual(
            calls,
            ["authenticate", "inspect", "lock", "inspect"],
        )

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
                [
                    "authenticate",
                    "inspect",
                    "lock",
                    "inspect",
                    "begin",
                    "stages",
                ],
            )

    def test_phase_flip_at_runner_lock_replays_terminal_after_unlock(self) -> None:
        from mlb_backtest.v52_acquire import (
            AcquisitionSummary,
            _AcquisitionPlan,
            _AcquireOps,
            _RuntimeState,
            _run_acquisition,
        )

        calls: list[str] = []
        locked = False
        states = iter(
            (
                _RuntimeState("DESIGN_ACCEPTED", False),
                _RuntimeState("SOURCES_READY", True),
            )
        )
        plan = _AcquisitionPlan(
            "a" * 64,
            ("b" * 64, "c" * 64, "d" * 64),
            ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
        )
        summary = AcquisitionSummary(
            contract_sha256=plan.contract_sha256,
            query_batch_sha256s=("e" * 64,) * 4,
            promoted=0,
            fetched=0,
            planned_promoted=1,
            planned_fetch_required=47,
            resumed=81,
            quarantined=0,
            pending=0,
            network_attempts=0,
        )

        @contextmanager
        def lock(_request):
            nonlocal locked
            calls.append("lock")
            locked = True
            try:
                yield
            finally:
                locked = False
                calls.append("unlock")

        def inspect(_request, _plan):
            calls.append("inspect")
            return next(states)

        def replay(_request, _plan):
            calls.append("replay")
            self.assertFalse(locked)
            return summary

        actual = _run_acquisition(
            self._request(plan_only=False, resume=False),
            _AcquireOps(
                authenticate=lambda _request: plan,
                runner_lock=lock,
                inspect_runtime=inspect,
                begin_runtime=lambda *_args: self.fail("stale begin reached"),
                replay_terminal=replay,
                run_stages=lambda *_args: self.fail("stale stages reached"),
            ),
        )
        self.assertEqual(actual, summary)
        self.assertEqual(
            calls,
            ["inspect", "lock", "inspect", "unlock", "replay"],
        )

    def test_terminal_promotion_replay_trust_fails_without_recovery_mutation(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _validate_terminal_remaining_promotion,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            query = _query()
            store = V52EvidenceStore(root)
            with store.acquire_query(query):
                pass
            recovery = (
                root
                / "queries"
                / query.source
                / logical_query_id(query)
                / "failure-transaction"
            )
            recovery.mkdir()
            (recovery / "canary").write_bytes(b"must-not-change")

            def snapshot() -> dict[Path, tuple[int, int, bytes | None]]:
                values: dict[Path, tuple[int, int, bytes | None]] = {}
                for path in (root, *sorted(root.rglob("*"))):
                    metadata = path.lstat()
                    values[path.relative_to(root)] = (
                        metadata.st_dev,
                        metadata.st_ino,
                        path.read_bytes() if path.is_file() else None,
                    )
                return values

            before = snapshot()
            with self.assertRaisesRegex(
                TrustIntegrityError,
                "terminal REMAINING promotion replay",
            ):
                _validate_terminal_remaining_promotion(
                    V52EvidenceStore.open_read_only(root),
                    ((query, object()),),
                )
            self.assertEqual(snapshot(), before)

    def test_real_terminal_partition_load_is_read_only_and_nonmutating(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _real_replay_terminal,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / "canary"
            marker.write_bytes(b"must-not-change")
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            plan = _AcquisitionPlan(
                "a" * 64,
                ("b" * 64, "c" * 64, "d" * 64),
                ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
            )
            checkpoint = SimpleNamespace(
                stage="REMAINING",
                pending_query_ids=(),
                quarantined_query_ids=(),
                stage_evidence_receipt_sha256s=("1" * 64,) * 10,
                query_batch_ids=(
                    plan.query_batch_ids[0],
                    "2" * 64,
                    "3" * 64,
                    "4" * 64,
                ),
                completed_query_ids=(),
            )
            calls: list[bool] = []

            def load_partition(
                _root: Path,
                _batch_id: str,
                *,
                read_only_progress: bool = False,
            ) -> object:
                calls.append(read_only_progress)
                return object()

            before = (marker.read_bytes(), marker.stat().st_ino)
            with (
                patch(
                    "mlb_backtest.execution_registry.load_terminal_acquisition_checkpoint",
                    return_value=checkpoint,
                ),
                patch(
                    "mlb_backtest.v52_query_plan.load_query_batch",
                    side_effect=load_partition,
                ),
                patch(
                    "mlb_backtest.v52_legacy_decision_inventory.load_verified_legacy_decision_inventory",
                    return_value=SimpleNamespace(payload={}),
                ),
                self.assertRaisesRegex(
                    TrustIntegrityError,
                    "protected decision authority",
                ),
            ):
                _real_replay_terminal(request, plan)

            self.assertEqual(calls, [True])
            self.assertEqual((marker.read_bytes(), marker.stat().st_ino), before)

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
                fetcher=lambda *_args, **_kwargs: self.fail(
                    "verified success refetched"
                ),
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
            self.assertEqual(
                (rerun.fetched, rerun.resumed, rerun.network_attempts), (0, 1, 0)
            )

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

    def test_real_stage_runner_seals_static_before_inventory_and_reference(
        self,
    ) -> None:
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
        inventory = SimpleNamespace(
            path=request.root / "schedule.json", sha256="1" * 64
        )
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
                side_effect=lambda *_args, **_kwargs: (
                    events.append("inventory") or inventory
                ),
            ),
            patch(
                "mlb_backtest.v52_acquire._validate_schedule_inventory_schema",
                side_effect=lambda *_args: events.append("schema"),
            ),
            patch(
                "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("build-reference") or reference
                ),
            ),
            patch("mlb_backtest.v52_acquire._execute_stage", side_effect=execute),
        ):
            summary = _real_run_stages(request, plan)

        self.assertLess(
            events.index("execute:STATIC_SCHEDULES"), events.index("inventory")
        )
        self.assertLess(events.index("inventory"), events.index("schema"))
        self.assertLess(events.index("schema"), events.index("build-reference"))
        self.assertLess(
            events.index("publish:reference"), events.index("execute:REFERENCE_INPUTS")
        )
        self.assertEqual(
            summary.query_batch_sha256s, (static.batch_id, reference.batch_id)
        )
        self.assertEqual(
            (summary.fetched, summary.resumed, summary.network_attempts), (18, 2, 18)
        )
        self.assertEqual((summary.pending, summary.quarantined), (0, 0))

    def test_preseason_disposition_completes_without_gate_or_network_attempt(
        self,
    ) -> None:
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
            result = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
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
        self.assertEqual(
            verified.published_evidence.storage_mode, "DERIVED_DISPOSITION"
        )

    def test_schedule_disposition_does_not_receive_generic_reason_semantics(
        self,
    ) -> None:
        from contextlib import nullcontext

        from mlb_backtest.v52_acquire import _AcquisitionPlan, _execute_stage

        query = dataclasses.replace(
            _query(),
            endpoint="disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            requested_range={"year": 2019},
        )
        query_id = logical_query_id(query)
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        request = self._request(plan_only=False, resume=True)
        plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
        expected_semantics = {
            "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
            "game_pk": 565534,
            "old_authority": {},
            "fixed_game_facts": {},
            "suppressed_occurrence": {},
            "retained_occurrence": {},
            "occurrence_counts": {},
            "normalized_games_sha256": "1" * 64,
            "selected_raw_game_object_sha256s_sha256": "2" * 64,
        }
        checkpoints: list[SimpleNamespace] = []

        def publish(**values):
            checkpoint = SimpleNamespace(
                receipt_sha256=f"{len(checkpoints) + 1:064x}", **values
            )
            checkpoints.append(checkpoint)
            return checkpoint

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            pending = SimpleNamespace(status="PENDING")
            success = SimpleNamespace(status="SUCCESS")
            with (
                patch(
                    "mlb_backtest.v52_acquire._source_disposition_authorities",
                    return_value={query_id: object()},
                ),
                patch.object(
                    store,
                    "read_verified_query_progress",
                    side_effect=(pending, pending, success, success),
                ),
                patch.object(store, "acquire_query", return_value=nullcontext()),
                patch.object(
                    store,
                    "publish_derived_disposition",
                    return_value=SimpleNamespace(query_id=query_id),
                ) as derived_writer,
            ):
                _execute_stage(
                    request,
                    plan,
                    store,
                    stage="STATIC_SCHEDULES",
                    batches=(batch,),
                    stage_evidence=(),
                    expected_prior=None,
                    fetcher=lambda *_args, **_kwargs: self.fail(
                        "schedule disposition must not fetch"
                    ),
                    checkpoint_publisher=publish,
                    parser=lambda _raw, _query: expected_semantics,
                )

        derived_writer.assert_called_once()
        self.assertEqual(derived_writer.call_args.args[1], expected_semantics)

    def test_recovery_dispositions_select_the_three_catalog_authorities(self) -> None:
        from mlb_backtest.protocols import (
            expected_recovery_source_run,
            parse_source_run_spec,
        )
        from mlb_backtest.v52_acquire import _source_disposition_authorities

        endpoints = (
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
            "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
        )
        queries = tuple(
            dataclasses.replace(
                _query(),
                source="mlb_schedule_game_feed",
                grain="season_schedule_and_game",
                endpoint=endpoint,
                parameters={},
                requested_range={"year": 2020 + index},
                parser_version="v5.2.2",
            )
            for index, endpoint in enumerate(endpoints)
        )
        authorities = tuple(SimpleNamespace(query=query) for query in queries)
        batch = SimpleNamespace(batch_id="e" * 64, queries=queries)
        request = dataclasses.replace(
            self._request(plan_only=True),
            run_spec=parse_source_run_spec(expected_recovery_source_run()),
        )
        with patch(
            "mlb_backtest.v52_schedule_conflict_recovery_disposition."
            "build_recovery_schedule_conflict_catalog",
            return_value=SimpleNamespace(entries=authorities),
        ):
            selected = _source_disposition_authorities(request, (batch,))
        self.assertEqual(
            selected,
            {
                logical_query_id(query): authority
                for query, authority in zip(queries, authorities)
            },
        )

    def test_legacy_v521_disposition_never_builds_recovery_catalog(self) -> None:
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_acquire import _source_disposition_authorities

        query = dataclasses.replace(
            _query(),
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint="disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
            parameters={},
            requested_range={"year": 2019},
            parser_version="v5.2.1",
        )
        legacy_authority = SimpleNamespace(query=query)
        request = dataclasses.replace(
            self._request(plan_only=True),
            run_spec=load_source_run_spec(
                ROOT / "protocols/v5_2_qd/"
                "historical_walkforward_500_superseding_565534.json"
            ),
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(query,))
        with (
            patch(
                "mlb_backtest.v52_schedule_conflict_recovery_disposition."
                "build_recovery_schedule_conflict_catalog",
                side_effect=AssertionError("legacy disposition selected recovery"),
            ),
            patch(
                "mlb_backtest.v52_duplicate_gamepk_disposition."
                "build_duplicate_disposition_authority",
                return_value=legacy_authority,
            ),
        ):
            selected = _source_disposition_authorities(request, (batch,))
        self.assertEqual(selected, {logical_query_id(query): legacy_authority})

    def test_stage_uses_exact_host_pools_and_publishes_only_after_workers_join(
        self,
    ) -> None:
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
            host = (
                "baseballsavant.mlb.com"
                if "savant" in query.endpoint
                else "statsapi.mlb.com"
            )
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

    def test_stage_unexpected_runtime_error_is_not_relabelled_or_checkpointed(
        self,
    ) -> None:
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
            with (
                self.subTest(fatal_type=fatal_type.__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
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
                        fetcher=lambda item, **_kwargs: (
                            fetches.append(logical_query_id(item)) or _capture()
                        ),
                        checkpoint_publisher=publish,
                        query_acquirer=acquire,
                        parser=parser,
                        network_gate=lambda _request, _plan, *, query, **_kwargs: (
                            gates.append(logical_query_id(query))
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
                    fetcher=lambda item, **_kwargs: (
                        fetches.append(logical_query_id(item)) or _capture()
                    ),
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
            with (
                self.subTest(delays=delays),
                tempfile.TemporaryDirectory() as directory,
            ):
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
            with (
                self.subTest(site=interrupt_site),
                tempfile.TemporaryDirectory() as directory,
            ):
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
                        fetcher=lambda item, **_kwargs: (
                            fetches.append(logical_query_id(item)) or _capture()
                        ),
                        checkpoint_publisher=publish,
                        query_acquirer=acquire,
                        parser=lambda _raw, _query: {},
                        network_gate=lambda _request, _plan, *, query, **_kwargs: (
                            gates.append(logical_query_id(query))
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
            free_bytes=lambda _root: (
                request.reserve_bytes + 2 * fetch_limits(sleeping).max_bytes
            ),
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
            with (
                self.subTest(
                trust_delay=trust_delay,
                programmer_delay=programmer_delay,
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
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

    def test_remaining_wave_joins_every_worker_before_returning_sorted_splits(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            SplitRequired,
            _AcquisitionPlan,
            _run_remaining_wave,
        )

        split = _query(statcast=True)
        completed = dataclasses.replace(
            _query(statcast=True),
            parameters={
                **dict(_query(statcast=True).parameters),
                "game_date_gt": "2024-06-01",
                "game_date_lt": "2024-06-01",
            },
            requested_range={
                "year": 2024,
                "start_date": "2024-06-01",
                "end_date": "2024-06-01",
                "acquisition": "FETCH_REQUIRED",
            },
        )
        batch = SimpleNamespace(batch_id="e" * 64, queries=(split, completed))
        release = threading.Event()
        success_finished = threading.Event()

        def acquire(store, item, parser, **_kwargs):
            if item == split:
                release.set()
                raise SplitRequired("TIMEOUT", 6)
            self.assertTrue(release.wait(timeout=2))
            time.sleep(0.05)
            with store.acquire_query(item) as lease:
                published = store.publish_fetched(
                    item,
                    store.next_attempt_number(lease),
                    _capture(b"{}"),
                    parser(b"{}", item),
                )
            success_finished.set()
            return published

        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            result = _run_remaining_wave(
                self._request(plan_only=False, resume=True),
                _AcquisitionPlan("a" * 64, (batch.batch_id,), ("FETCH_REQUIRED",)),
                store,
                batches=(batch,),
                active_queries=batch.queries,
                fetcher=lambda *_args, **_kwargs: self.fail("fake acquirer fetched"),
                query_acquirer=acquire,
                parser=lambda _raw, _query: {},
                network_gate=lambda *_args, **_kwargs: None,
            )

        self.assertTrue(success_finished.is_set())
        self.assertEqual(result.fetched, 0)
        self.assertEqual(result.resumed, 0)
        self.assertEqual(result.network_attempts, 0)
        self.assertEqual(
            result.splits,
            ((logical_query_id(split), SplitRequired("TIMEOUT", 6)),),
        )

    def test_remaining_stage_evidence_is_the_exact_sorted_ten_path_authority(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import _remaining_stage_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            participant = tuple(
                root / relative
                for relative in (
                    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
                    "config/v5_2_qd/venue_timezones.json",
                    "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json",
                )
            )
            root_batch_id = "a" * 64
            actual = _remaining_stage_evidence(root, participant, root_batch_id)
            expected = tuple(
                sorted(
                    (
                        *participant,
                        root / "audit/v5_1_bt/protected_raw_inventory.json",
                        root
                        / "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json",
                        root
                        / "audit/v5_2_qd/historical/acquisition/query_batches"
                        / f"{root_batch_id}.json",
                    ),
                    key=os.fspath,
                )
            )
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 10)

    def test_remaining_population_classifies_split_parent_outside_pending(self) -> None:
        from mlb_backtest.v52_acquire import _remaining_populations

        parent = _query(statcast=True)
        child = dataclasses.replace(
            _query(statcast=True),
            parameters={
                **dict(_query(statcast=True).parameters),
                "game_date_gt": "2024-06-01",
                "game_date_lt": "2024-06-01",
            },
            requested_range={
                "year": 2024,
                "start_date": "2024-06-01",
                "end_date": "2024-06-01",
                "acquisition": "FETCH_REQUIRED",
            },
        )
        batch = SimpleNamespace(batch_id="b" * 64, queries=(parent, child))
        parent_id = logical_query_id(parent)
        child_id = logical_query_id(child)
        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            completed, pending, quarantined, split = _remaining_populations(
                store,
                (batch,),
                (parent_id,),
            )
        self.assertEqual(completed, ())
        self.assertEqual(pending, (child_id,))
        self.assertEqual(quarantined, ())
        self.assertEqual(split, (parent_id,))

    def test_remaining_decisions_require_exact_fresh_one_of_forty_eight_promotion(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _validate_remaining_decisions,
        )
        from mlb_backtest.v52_legacy import load_protected_legacy_catalog
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from tests.test_v52_legacy_decision_inventory import (
            LEGACY_ROOT,
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(Path(directory))
            root_batch = build_statcast_root_batch(root)
            published_root = publish_query_batch(root, root_batch)
            self.assertTrue(published_root.is_file())
            inventory = publish_legacy_decision_inventory(root)
            partition = build_legacy_partition_batch(root)
            catalog = load_protected_legacy_catalog(
                root,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=inventory_path,
                protected_inventory_sha256=digest,
            )
            promoted = _validate_remaining_decisions(partition, inventory, catalog)
            self.assertEqual(len(promoted), 1)
            self.assertEqual(
                promoted[0][0].requested_range["acquisition"],
                "PROTECTED_LEGACY_REFERENCE",
            )

            payload = dict(inventory.payload)
            decisions = list(payload["decisions"])
            decisions[0] = {**dict(decisions[0]), "disposition": "PROMOTED"}
            payload["decisions"] = tuple(decisions)
            forged = SimpleNamespace(payload=payload)
            with self.assertRaisesRegex(TrustIntegrityError, "decision"):
                _validate_remaining_decisions(partition, forged, catalog)

    def test_remaining_promotion_failure_is_trust_and_has_no_fetch_fallback(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _promote_remaining,
            _validate_remaining_decisions,
        )
        from mlb_backtest.v52_legacy import load_protected_legacy_catalog
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from tests.test_v52_legacy_decision_inventory import (
            LEGACY_ROOT,
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(Path(directory))
            publish_query_batch(root, build_statcast_root_batch(root))
            inventory = publish_legacy_decision_inventory(root)
            partition = build_legacy_partition_batch(root)
            catalog = load_protected_legacy_catalog(
                root,
                legacy_root=root / LEGACY_ROOT,
                protected_inventory_path=inventory_path,
                protected_inventory_sha256=digest,
            )
            promoted = _validate_remaining_decisions(partition, inventory, catalog)
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            with (
                patch(
                    "mlb_backtest.v52_legacy.promote_legacy_candidate",
                    side_effect=RuntimeError("promotion drift"),
                ) as promote,
                self.assertRaisesRegex(TrustIntegrityError, "promotion"),
            ):
                _promote_remaining(store, catalog, promoted)
            promote.assert_called_once()
            self.assertEqual(
                store.read_verified_query_progress(promoted[0][0]).status,
                "PENDING",
            )

    def test_terminal_remaining_requires_protected_reference_storage_mode(self) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _validate_terminal_remaining_promotion,
        )
        from mlb_backtest.v52_evidence import PublishedEvidence

        query = _query(statcast=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.mkdir(exist_ok=True)
            store = V52EvidenceStore(root / "raw")
            before = tuple(root.rglob("*"))
            for mode, error in (
                ("FETCHED_RAW", TrustIntegrityError),
                ("PROTECTED_LEGACY_REFERENCE", None),
            ):
                with self.subTest(mode=mode):
                    progress = SimpleNamespace(
                        status="SUCCESS",
                        published_evidence=PublishedEvidence(
                            query_id=logical_query_id(query),
                            success_receipt_path=root / "not-read.json",
                            raw_sha256="a" * 64,
                            query_sidecar_sha256="b" * 64,
                            storage_mode=mode,
                        ),
                    )
                    context = (
                        self.assertRaisesRegex(TrustIntegrityError, "promotion|storage")
                        if error is not None
                        else nullcontext()
                    )
                    with (
                        patch.object(
                            store,
                            "read_verified_query_progress",
                            return_value=progress,
                        ),
                        context,
                    ):
                        _validate_terminal_remaining_promotion(
                            store,
                            ((query, object()),),
                        )
            after = tuple(root.rglob("*"))
        self.assertEqual(after, before)

    def test_remaining_fixed_four_checkpoints_promotion_before_fetch(self) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _DiskBudget,
            _RemainingWaveResult,
            _RunnerGuard,
            _run_remaining_stage,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            statcast_root = build_statcast_root_batch(root)
            # Preview construction is authenticated by the production helper; this
            # placeholder merely gives the plan its already-sealed partition ID.
            from mlb_backtest.v52_legacy import load_protected_legacy_catalog
            from mlb_backtest.v52_legacy_decision_inventory import (
                preview_legacy_decisions,
            )

            catalog = load_protected_legacy_catalog(
                root,
                legacy_root=root / "data/v5_1_bt/raw",
                protected_inventory_path=root
                / "audit/v5_1_bt/protected_raw_inventory.json",
                protected_inventory_sha256=_digest,
            )
            preview = preview_legacy_decisions(
                statcast_root,
                catalog,
                protected_inventory_sha256=_digest,
            )
            plan = _AcquisitionPlan(
                "a" * 64,
                ("1" * 64, statcast_root.batch_id, preview.partition_batch.batch_id),
                ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
            )
            fixed = tuple(
                SimpleNamespace(batch_id=character * 64, queries=())
                for character in "234"
            )
            latest = SimpleNamespace(
                stage="PARTICIPANT_DERIVED",
                receipt_sha256="5" * 64,
                query_batch_ids=tuple(batch.batch_id for batch in fixed),
                completed_query_ids=(),
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            complete: set[str] = set()
            events: list[str] = []
            checkpoint_number = 0

            def populations(_store, batches, split_ids):
                partition_ids = {
                    logical_query_id(query) for query in batches[3].queries
                }
                return (
                    tuple(sorted(complete)),
                    tuple(sorted(partition_ids - complete)),
                    (),
                    tuple(sorted(split_ids)),
                )

            def checkpoint(_root, _run_spec, **kwargs):
                nonlocal checkpoint_number
                checkpoint_number += 1
                events.append("checkpoint")
                return SimpleNamespace(
                    receipt_sha256=f"{checkpoint_number + 5:064x}",
                    **kwargs,
                )

            def promote(_store, _catalog, promoted):
                events.append("promote")
                complete.add(logical_query_id(promoted[0][0]))
                return (1, 0)

            def wave(_request, _plan, _store, *, active_queries, **_kwargs):
                events.append("fetch-wave")
                self.assertTrue(complete)
                complete.update(logical_query_id(query) for query in active_queries)
                return _RemainingWaveResult(
                    len(active_queries), 0, len(active_queries), ()
                )

            with (
                patch(
                    "mlb_backtest.v52_acquire._remaining_populations",
                    side_effect=populations,
                ),
                patch(
                    "mlb_backtest.v52_acquire._promote_remaining",
                    side_effect=promote,
                ),
                patch(
                    "mlb_backtest.v52_acquire._run_remaining_wave",
                    side_effect=wave,
                ),
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=checkpoint,
                ),
            ):
                result = _run_remaining_stage(
                    request,
                    plan,
                    store,
                    fixed_prefix=fixed,
                    participant_evidence=tuple(
                        root / f"evidence-{index}" for index in range(7)
                    ),
                    latest=latest,
                    evidence_root=store.root,
                    runner_guard=_RunnerGuard(10, 11, root),
                    disk_budget=_DiskBudget(
                        request,
                        free_bytes=lambda _root: request.reserve_bytes + 10**10,
                    ),
                )
        self.assertEqual(
            events, ["checkpoint", "promote", "checkpoint", "fetch-wave", "checkpoint"]
        )
        self.assertEqual(result.promoted, 1)
        self.assertEqual(result.fetched, 47)
        self.assertEqual(len(result.query_batch_ids), 4)

    def test_remaining_split_materializes_child_before_receipt_and_reconciles(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            SplitRequired,
            _materialize_remaining_split,
        )
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            publish_query_batch(root, build_statcast_root_batch(root))
            publish_legacy_decision_inventory(root)
            partition = build_legacy_partition_batch(root)
            publish_query_batch(root, partition)
            parent = next(
                query
                for query in partition.queries
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and query.requested_range["start_date"]
                != query.requested_range["end_date"]
            )
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            capture = dataclasses.replace(
                _capture(b"partial", error="SIZE_LIMIT"),
                final_url="https://baseballsavant.mlb.com/statcast_search/csv",
                response_headers=(("Content-Type", "text/csv"),),
            )
            with store.acquire_query(parent) as lease:
                number = store.next_attempt_number(lease)
                store.persist_split_attempt(
                    parent,
                    number,
                    capture,
                    reason="SIZE_LIMIT",
                )
            events: list[str] = []
            from mlb_backtest import v52_query_plan as query_plan

            real_publish_batch = query_plan.publish_query_batch
            real_publish_receipt = query_plan.publish_split_receipt
            interrupt_receipt = True

            def publish_batch(*args, **kwargs):
                events.append("child-batch")
                return real_publish_batch(*args, **kwargs)

            def publish_receipt(*args, **kwargs):
                nonlocal interrupt_receipt
                events.append("split-receipt")
                if interrupt_receipt:
                    raise KeyboardInterrupt("after child batch")
                return real_publish_receipt(*args, **kwargs)

            with (
                patch(
                    "mlb_backtest.v52_query_plan.publish_query_batch",
                    side_effect=publish_batch,
                ),
                patch(
                    "mlb_backtest.v52_query_plan.publish_split_receipt",
                    side_effect=publish_receipt,
                ),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "after child batch"):
                    _materialize_remaining_split(
                        root,
                        store.root,
                        partition,
                        parent,
                        SplitRequired("SIZE_LIMIT", number),
                    )
                interrupt_receipt = False
                child, receipt = _materialize_remaining_split(
                    root,
                    store.root,
                    partition,
                    parent,
                    SplitRequired("SIZE_LIMIT", number),
                )
                first_files = {
                    path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                }
                repeated = _materialize_remaining_split(
                    root,
                    store.root,
                    partition,
                    parent,
                    SplitRequired("SIZE_LIMIT", number),
                )
                second_files = {
                    path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                }
                recursive_parent = next(
                    query
                    for query in child.queries
                    if query.requested_range["start_date"]
                    != query.requested_range["end_date"]
                )
                with store.acquire_query(recursive_parent) as lease:
                    recursive_number = store.next_attempt_number(lease)
                    store.persist_split_attempt(
                        recursive_parent,
                        recursive_number,
                        capture,
                        reason="SIZE_LIMIT",
                    )
                recursive_child, recursive_receipt = _materialize_remaining_split(
                    root,
                    store.root,
                    child,
                    recursive_parent,
                    SplitRequired("SIZE_LIMIT", recursive_number),
                )
        self.assertEqual(
            events[:4],
            ["child-batch", "split-receipt", "child-batch", "split-receipt"],
        )
        self.assertEqual(
            events[4:],
            ["child-batch", "child-batch", "split-receipt"],
        )
        self.assertEqual(repeated, (child, receipt))
        self.assertEqual(second_files, first_files)
        self.assertEqual(recursive_child.derived_from_sha256, child.batch_id)
        self.assertEqual(recursive_receipt.depth, 2)

    def test_terminal_remaining_replay_is_byte_idempotent_and_zero_network(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _AcquisitionPlan,
            _DiskBudget,
            _RunnerGuard,
            _run_remaining_stage,
        )
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_evidence import PublishedEvidence
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            statcast_root = build_statcast_root_batch(root)
            publish_query_batch(root, statcast_root)
            publish_legacy_decision_inventory(root)
            partition = build_legacy_partition_batch(root)
            publish_query_batch(root, partition)
            fixed = tuple(
                SimpleNamespace(batch_id=character * 64, queries=())
                for character in "234"
            )
            completed = tuple(
                sorted(logical_query_id(query) for query in partition.queries)
            )
            latest = SimpleNamespace(
                stage="REMAINING",
                receipt_sha256="5" * 64,
                query_batch_ids=(
                    *tuple(batch.batch_id for batch in fixed),
                    partition.batch_id,
                ),
                completed_query_ids=completed,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            promoted_query = next(
                query
                for query in partition.queries
                if query.requested_range["acquisition"] == "PROTECTED_LEGACY_REFERENCE"
            )
            terminal_mode = {"value": "FETCHED_RAW"}

            def terminal_progress(_query):
                return SimpleNamespace(
                    status="SUCCESS",
                    published_evidence=PublishedEvidence(
                        query_id=logical_query_id(promoted_query),
                        success_receipt_path=root
                        / "authenticated-by-public-progress.json",
                        raw_sha256="a" * 64,
                        query_sidecar_sha256="b" * 64,
                        storage_mode=terminal_mode["value"],
                    ),
                )

            before = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            }
            with (
                patch(
                    "mlb_backtest.v52_acquire._remaining_populations",
                    return_value=(completed, (), (), ()),
                ),
                patch.object(
                    store,
                    "read_verified_query_progress",
                    side_effect=terminal_progress,
                ),
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=AssertionError("terminal checkpoint republished"),
                ),
                patch(
                    "mlb_backtest.v52_acquire._promote_remaining",
                    side_effect=AssertionError("terminal promotion replayed"),
                ),
                patch(
                    "mlb_backtest.v52_acquire._run_remaining_wave",
                    side_effect=AssertionError("terminal network wave reached"),
                ),
            ):
                with self.assertRaisesRegex(TrustIntegrityError, "promotion|storage"):
                    _run_remaining_stage(
                        request,
                        _AcquisitionPlan(
                            "a" * 64,
                            ("1" * 64, statcast_root.batch_id, "6" * 64),
                            ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
                        ),
                        store,
                        fixed_prefix=fixed,
                        participant_evidence=tuple(
                            root / f"evidence-{index}" for index in range(7)
                        ),
                        latest=latest,
                        evidence_root=store.root,
                        runner_guard=_RunnerGuard(10, 11, root),
                        disk_budget=_DiskBudget(
                            request,
                            free_bytes=lambda _root: request.reserve_bytes + 10**10,
                        ),
                    )
                self.assertEqual(
                    {
                        path.relative_to(root): (
                            path.read_bytes(),
                            path.stat().st_ino,
                        )
                        for path in root.rglob("*")
                        if path.is_file()
                    },
                    before,
                )
                terminal_mode["value"] = "PROTECTED_LEGACY_REFERENCE"
                result = _run_remaining_stage(
                    request,
                    _AcquisitionPlan(
                        "a" * 64,
                        ("1" * 64, statcast_root.batch_id, "6" * 64),
                        ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
                    ),
                    store,
                    fixed_prefix=fixed,
                    participant_evidence=tuple(
                        root / f"evidence-{index}" for index in range(7)
                    ),
                    latest=latest,
                    evidence_root=store.root,
                    runner_guard=_RunnerGuard(10, 11, root),
                    disk_budget=_DiskBudget(
                        request,
                        free_bytes=lambda _root: request.reserve_bytes + 10**10,
                    ),
                )
            after = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            }
        self.assertEqual(after, before)
        self.assertEqual(result.query_batch_ids, latest.query_batch_ids)
        self.assertEqual((result.fetched, result.network_attempts), (0, 0))

    def test_remaining_repairs_stale_pending_once_after_terminal_checkpoint_crash(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _DiskBudget,
            _RunnerGuard,
            _run_remaining_stage,
        )
        from mlb_backtest.v52_evidence import PublishedEvidence
        from mlb_backtest.v52_legacy_decision_inventory import (
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch,
            build_statcast_root_batch,
            publish_query_batch,
        )
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            statcast_root = build_statcast_root_batch(root)
            publish_query_batch(root, statcast_root)
            publish_legacy_decision_inventory(root)
            partition = build_legacy_partition_batch(root)
            publish_query_batch(root, partition)
            fixed = tuple(
                SimpleNamespace(batch_id=character * 64, queries=())
                for character in "234"
            )
            completed = tuple(
                sorted(logical_query_id(query) for query in partition.queries)
            )
            stale_pending_id = logical_query_id(
                next(
                    query
                    for query in partition.queries
                    if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                )
            )
            stale_completed = tuple(
                query_id for query_id in completed if query_id != stale_pending_id
            )
            self.assertFalse(set(stale_completed) & {stale_pending_id})
            self.assertEqual(
                set(stale_completed) | {stale_pending_id},
                set(completed),
            )
            stale = SimpleNamespace(
                stage="REMAINING",
                receipt_sha256="5" * 64,
                query_batch_ids=(
                    *tuple(batch.batch_id for batch in fixed),
                    partition.batch_id,
                ),
                completed_query_ids=stale_completed,
                pending_query_ids=(stale_pending_id,),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            promoted_id = logical_query_id(
                next(
                    query
                    for query in partition.queries
                    if query.requested_range["acquisition"]
                    == "PROTECTED_LEGACY_REFERENCE"
                )
            )
            publications: list[SimpleNamespace] = []
            crash_before_checkpoint = True

            def progress(query):
                query_id = logical_query_id(query)
                return SimpleNamespace(
                    status="SUCCESS",
                    published_evidence=PublishedEvidence(
                        query_id=query_id,
                        success_receipt_path=root / f"{query_id}.success.json",
                        raw_sha256="a" * 64,
                        query_sidecar_sha256="b" * 64,
                        storage_mode=(
                            "PROTECTED_LEGACY_REFERENCE"
                            if query_id == promoted_id
                            else "FETCHED_RAW"
                        ),
                    ),
                )

            def checkpoint(_root, _run_spec, **kwargs):
                nonlocal crash_before_checkpoint
                if crash_before_checkpoint:
                    crash_before_checkpoint = False
                    raise KeyboardInterrupt("after all success before final checkpoint")
                value = SimpleNamespace(receipt_sha256="6" * 64, **kwargs)
                publications.append(value)
                return value

            shared = {
                "fixed_prefix": fixed,
                "participant_evidence": tuple(
                    root / f"evidence-{index}" for index in range(7)
                ),
                "evidence_root": store.root,
                "runner_guard": _RunnerGuard(10, 11, root),
                "disk_budget": _DiskBudget(
                    request,
                    free_bytes=lambda _root: request.reserve_bytes + 10**10,
                ),
            }
            plan = _AcquisitionPlan(
                "a" * 64,
                ("1" * 64, statcast_root.batch_id, "6" * 64),
                ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
            )
            before = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            }
            with (
                patch(
                    "mlb_backtest.v52_acquire._remaining_populations",
                    return_value=(completed, (), (), ()),
                ),
                patch.object(
                    store,
                    "read_verified_query_progress",
                    side_effect=progress,
                ),
                patch(
                    "mlb_backtest.v52_acquire._promote_remaining",
                    return_value=(0, 1),
                ) as promotion,
                patch(
                    "mlb_backtest.v52_acquire._run_remaining_wave",
                    side_effect=AssertionError("successful query refetched"),
                ),
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=checkpoint,
                ) as publication,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "after all success before final checkpoint",
                ):
                    _run_remaining_stage(
                        request,
                        plan,
                        store,
                        latest=stale,
                        **shared,
                    )
                corrected = _run_remaining_stage(
                    request,
                    plan,
                    store,
                    latest=stale,
                    **shared,
                )
                self.assertEqual(len(publications), 1)
                self.assertEqual(publications[0].pending_query_ids, ())
                self.assertEqual(promotion.call_count, 2)
                corrective_publication_calls = publication.call_count
                replayed = _run_remaining_stage(
                    request,
                    plan,
                    store,
                    latest=corrected.checkpoint,
                    **shared,
                )
                self.assertEqual(publication.call_count, corrective_publication_calls)
                self.assertEqual(promotion.call_count, 2)
            after = {
                path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(after, before)
        self.assertIs(replayed.checkpoint, corrected.checkpoint)
        self.assertEqual((corrected.fetched, corrected.network_attempts), (0, 0))
        self.assertEqual((replayed.fetched, replayed.network_attempts), (0, 0))

    def test_remaining_receipt_checkpoint_child_fetch_barriers_resume_after_crash(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _DiskBudget,
            _RemainingWaveResult,
            _RunnerGuard,
            _materialize_remaining_split,
            _run_remaining_stage,
        )
        from mlb_backtest.v52_legacy import load_protected_legacy_catalog
        from mlb_backtest.v52_legacy_decision_inventory import (
            preview_legacy_decisions,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch
        from tests.test_v52_legacy_decision_inventory import (
            _legacy_decision_repository,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _legacy_decision_repository(Path(directory))
            statcast_root = build_statcast_root_batch(root)
            catalog = load_protected_legacy_catalog(
                root,
                legacy_root=root / "data/v5_1_bt/raw",
                protected_inventory_path=root
                / "audit/v5_1_bt/protected_raw_inventory.json",
                protected_inventory_sha256=digest,
            )
            preview = preview_legacy_decisions(
                statcast_root,
                catalog,
                protected_inventory_sha256=digest,
            )
            plan = _AcquisitionPlan(
                "a" * 64,
                ("1" * 64, statcast_root.batch_id, preview.partition_batch.batch_id),
                ("PROMOTED", *("FETCH_REQUIRED" for _ in range(47))),
            )
            fixed = tuple(
                SimpleNamespace(batch_id=character * 64, queries=())
                for character in "234"
            )
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            (root / "data/v5_2_qd/historical").mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            parent = next(
                query
                for query in preview.partition_batch.queries
                if query.requested_range["acquisition"] == "FETCH_REQUIRED"
                and query.requested_range["start_date"]
                != query.requested_range["end_date"]
            )
            capture = dataclasses.replace(
                _capture(b"partial", error="SIZE_LIMIT"),
                final_url="https://baseballsavant.mlb.com/statcast_search/csv",
                response_headers=(("Content-Type", "text/csv"),),
            )
            with store.acquire_query(parent) as lease:
                attempt = store.next_attempt_number(lease)
                store.persist_split_attempt(
                    parent,
                    attempt,
                    capture,
                    reason="SIZE_LIMIT",
                )
            parent_id = logical_query_id(parent)
            promoted_id = next(
                logical_query_id(query)
                for query in preview.partition_batch.queries
                if query.requested_range["acquisition"] == "PROTECTED_LEGACY_REFERENCE"
            )
            complete: set[str] = set()
            durable_checkpoints: list[SimpleNamespace] = []
            events: list[str] = []
            crash_split_checkpoint = True

            def populations(_store, batches, split_ids):
                query_ids = {
                    logical_query_id(query)
                    for batch in batches[3:]
                    for query in batch.queries
                }
                split = set(split_ids)
                return (
                    tuple(sorted(complete)),
                    tuple(sorted(query_ids - split - complete)),
                    (),
                    tuple(sorted(split)),
                )

            def promote(_store, _catalog, _promoted):
                resumed = promoted_id in complete
                complete.add(promoted_id)
                events.append("promotion")
                return (0, 1) if resumed else (1, 0)

            def checkpoint(_root, _run_spec, **kwargs):
                nonlocal crash_split_checkpoint
                split = tuple(kwargs["split_query_ids"])
                pending = tuple(kwargs["pending_query_ids"])
                if split and crash_split_checkpoint:
                    events.append("checkpoint-crash")
                    crash_split_checkpoint = False
                    raise KeyboardInterrupt("after receipt before checkpoint")
                label = (
                    "split-checkpoint"
                    if split and pending
                    else "terminal-checkpoint"
                    if split
                    else "fixed-checkpoint"
                )
                events.append(label)
                value = SimpleNamespace(
                    receipt_sha256=f"{len(durable_checkpoints) + 6:064x}",
                    **kwargs,
                )
                durable_checkpoints.append(value)
                return value

            real_materialize = _materialize_remaining_split

            def materialize(*args, **kwargs):
                value = real_materialize(*args, **kwargs)
                events.append("receipt-ready")
                return value

            wave_calls = 0

            def wave(_request, _plan, _store, *, active_queries, **_kwargs):
                nonlocal wave_calls
                wave_calls += 1
                events.append("child-fetch")
                self.assertIn(parent_id, durable_checkpoints[-1].split_query_ids)
                complete.update(logical_query_id(query) for query in active_queries)
                return _RemainingWaveResult(
                    len(active_queries), 0, len(active_queries), ()
                )

            initial = SimpleNamespace(
                stage="PARTICIPANT_DERIVED",
                receipt_sha256="5" * 64,
                query_batch_ids=tuple(batch.batch_id for batch in fixed),
                completed_query_ids=(),
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            shared = {
                "fixed_prefix": fixed,
                "participant_evidence": tuple(
                    root / f"evidence-{index}" for index in range(7)
                ),
                "evidence_root": store.root,
                "runner_guard": _RunnerGuard(10, 11, root),
                "disk_budget": _DiskBudget(
                    request,
                    free_bytes=lambda _root: request.reserve_bytes + 10**10,
                ),
            }
            with (
                patch(
                    "mlb_backtest.v52_acquire._remaining_populations",
                    side_effect=populations,
                ),
                patch(
                    "mlb_backtest.v52_acquire._promote_remaining",
                    side_effect=promote,
                ),
                patch(
                    "mlb_backtest.v52_acquire._materialize_remaining_split",
                    side_effect=materialize,
                ),
                patch(
                    "mlb_backtest.v52_acquire._run_remaining_wave",
                    side_effect=wave,
                ),
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=checkpoint,
                ),
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "after receipt before checkpoint"
                ):
                    _run_remaining_stage(
                        request,
                        plan,
                        store,
                        latest=initial,
                        **shared,
                    )
                resume_from = durable_checkpoints[-1]
                result = _run_remaining_stage(
                    request,
                    plan,
                    store,
                    latest=resume_from,
                    **shared,
                )
        first_receipt = events.index("receipt-ready")
        crash = events.index("checkpoint-crash")
        second_receipt = events.index("receipt-ready", first_receipt + 1)
        split_checkpoint = events.index("split-checkpoint")
        child_fetch = events.index("child-fetch")
        terminal_checkpoint = events.index("terminal-checkpoint")
        self.assertLess(first_receipt, crash)
        self.assertLess(crash, second_receipt)
        self.assertLess(second_receipt, split_checkpoint)
        self.assertLess(split_checkpoint, child_fetch)
        self.assertLess(child_fetch, terminal_checkpoint)
        self.assertEqual(wave_calls, 1)
        self.assertEqual(len(result.query_batch_ids), 5)

    def test_mixed_split_quarantine_repairs_split_before_operational_resume(
        self,
    ) -> None:
        from mlb_backtest import v52_legacy_decision_inventory as inventory_module
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TerminalAcquisitionFailure,
            _AcquisitionPlan,
            _DiskBudget,
            _RunnerGuard,
            _run_remaining_stage,
        )

        # Load its query-plan aliases before patching the source module below.
        self.assertTrue(callable(inventory_module.publish_legacy_decision_inventory))

        promoted = _query(statcast=True, single_day=True)
        split_parent = dataclasses.replace(
            _query(statcast=True),
            parameters={"game_date_gt": "2024-05-01", "game_date_lt": "2024-05-04"},
            requested_range={
                "acquisition": "FETCH_REQUIRED",
                "end_date": "2024-05-04",
                "start_date": "2024-05-01",
                "year": 2024,
            },
        )
        quarantined_query = dataclasses.replace(
            _query(statcast=True, single_day=True),
            parameters={"game_date_gt": "2024-06-01", "game_date_lt": "2024-06-01"},
            requested_range={
                "acquisition": "FETCH_REQUIRED",
                "end_date": "2024-06-01",
                "start_date": "2024-06-01",
                "year": 2024,
            },
        )
        promoted = dataclasses.replace(
            promoted,
            requested_range={
                **dict(promoted.requested_range),
                "acquisition": "PROTECTED_LEGACY_REFERENCE",
                "protected_inventory_sha256": "9" * 64,
            },
        )
        partition = SimpleNamespace(
            batch_id="8" * 64,
            queries=(promoted, split_parent, quarantined_query),
        )
        root_batch = SimpleNamespace(batch_id="7" * 64, queries=())
        child_query = dataclasses.replace(
            split_parent,
            parameters={"game_date_gt": "2024-05-01", "game_date_lt": "2024-05-02"},
            requested_range={
                **dict(split_parent.requested_range),
                "end_date": "2024-05-02",
            },
            parent_query_id=logical_query_id(split_parent),
        )
        child_batch = SimpleNamespace(
            batch_id="6" * 64,
            derived_from_sha256=partition.batch_id,
            queries=(child_query,),
        )
        split_parent_id = logical_query_id(split_parent)
        quarantine_id = logical_query_id(quarantined_query)
        receipt = SimpleNamespace(
            parent_batch_id=partition.batch_id,
            parent_query_id=split_parent_id,
            reason="SIZE_LIMIT",
            exhausted_attempt_number=1,
            child_batch_id=child_batch.batch_id,
            child_query_ids=(logical_query_id(child_query),),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "data/v5_2_qd/historical").mkdir(parents=True)
            store = V52EvidenceStore(root / "data/v5_2_qd/historical/raw")
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True),
                root=root,
            )
            fixed = tuple(
                SimpleNamespace(batch_id=character * 64, queries=())
                for character in "234"
            )
            complete = {logical_query_id(promoted)}
            checkpoints: list[SimpleNamespace] = []
            materialize_calls = 0
            wave_calls = 0

            def populations(selected_store, batches, split_ids):
                completed: list[str] = []
                pending: list[str] = []
                quarantined: list[str] = []
                split = set(split_ids)
                for batch in batches[3:]:
                    for query in batch.queries:
                        query_id = logical_query_id(query)
                        if query_id in split:
                            continue
                        progress = selected_store.read_verified_query_progress(query)
                        if query_id in complete or progress.status == "SUCCESS":
                            completed.append(query_id)
                        elif progress.status == "QUARANTINED":
                            quarantined.append(query_id)
                        else:
                            pending.append(query_id)
                return (
                    tuple(sorted(completed)),
                    tuple(sorted(pending)),
                    tuple(sorted(quarantined)),
                    tuple(sorted(split)),
                )

            def checkpoint(_root, _run_spec, **kwargs):
                value = SimpleNamespace(
                    receipt_sha256=f"{len(checkpoints) + 10:064x}",
                    **kwargs,
                )
                checkpoints.append(value)
                return value

            def mixed_wave(*_args, **_kwargs):
                nonlocal wave_calls
                wave_calls += 1
                self.assertEqual(wave_calls, 1, "split parent was refetched")
                split_capture = dataclasses.replace(
                    _capture(b"partial", error="SIZE_LIMIT"),
                    final_url="https://baseballsavant.mlb.com/statcast_search/csv",
                    response_headers=(("Content-Type", "text/csv"),),
                )
                with store.acquire_query(split_parent) as lease:
                    split_attempt = store.next_attempt_number(lease)
                    store.persist_split_attempt(
                        split_parent,
                        split_attempt,
                        split_capture,
                        reason="SIZE_LIMIT",
                    )
                quarantine_capture = dataclasses.replace(
                    _capture(b"bad", status=404, error="PERMANENT_HTTP"),
                    final_url="https://baseballsavant.mlb.com/statcast_search/csv",
                )
                with store.acquire_query(quarantined_query) as lease:
                    quarantine_attempt = store.next_attempt_number(lease)
                    store.quarantine_attempt(
                        quarantined_query,
                        quarantine_attempt,
                        quarantine_capture,
                        error_classification="PERMANENT_HTTP",
                    )
                raise TerminalAcquisitionFailure("PERMANENT_HTTP", quarantine_attempt)

            artifact_root = root / "repair-artifacts"

            def materialize(*_args, **_kwargs):
                nonlocal materialize_calls
                materialize_calls += 1
                if materialize_calls == 1:
                    raise KeyboardInterrupt("during mixed split repair")
                artifact_root.mkdir(exist_ok=True)
                for name, contents in (
                    ("child.json", b"child\n"),
                    ("receipt.json", b"receipt\n"),
                ):
                    path = artifact_root / name
                    if not path.exists():
                        path.write_bytes(contents)
                return child_batch, receipt

            batch_map = {
                root_batch.batch_id: root_batch,
                partition.batch_id: partition,
                child_batch.batch_id: child_batch,
            }
            initial = SimpleNamespace(
                stage="PARTICIPANT_DERIVED",
                receipt_sha256="5" * 64,
                query_batch_ids=tuple(batch.batch_id for batch in fixed),
                completed_query_ids=(),
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            shared = {
                "fixed_prefix": fixed,
                "participant_evidence": tuple(
                    root / f"evidence-{index}" for index in range(7)
                ),
                "evidence_root": store.root,
                "runner_guard": _RunnerGuard(10, 11, root),
                "disk_budget": _DiskBudget(
                    request,
                    free_bytes=lambda _root: request.reserve_bytes + 10**10,
                ),
            }
            patches = (
                patch(
                    "mlb_backtest.v52_query_plan.build_statcast_root_batch",
                    return_value=root_batch,
                ),
                patch(
                    "mlb_backtest.v52_query_plan.build_legacy_partition_batch",
                    return_value=partition,
                ),
                patch(
                    "mlb_backtest.v52_query_plan.load_query_batch",
                    side_effect=lambda _root, batch_id: batch_map[batch_id],
                ),
                patch("mlb_backtest.v52_query_plan.publish_query_batch"),
                patch(
                    "mlb_backtest.v52_query_plan.load_split_receipt",
                    side_effect=lambda _root, parent_id, **_kwargs: (
                        receipt if parent_id == split_parent_id else None
                    ),
                ),
                patch(
                    "mlb_backtest.v52_legacy_decision_inventory.publish_legacy_decision_inventory",
                    return_value=SimpleNamespace(
                        payload={"protected_inventory": {"sha256": "9" * 64}}
                    ),
                ),
                patch(
                    "mlb_backtest.v52_legacy_decision_inventory.load_verified_legacy_decision_inventory",
                    return_value=SimpleNamespace(
                        payload={"protected_inventory": {"sha256": "9" * 64}}
                    ),
                ),
                patch(
                    "mlb_backtest.v52_legacy.load_protected_legacy_catalog",
                    return_value=object(),
                ),
                patch(
                    "mlb_backtest.v52_acquire._validate_remaining_decisions",
                    return_value=((promoted, object()),),
                ),
                patch(
                    "mlb_backtest.v52_acquire._promote_remaining",
                    return_value=(0, 1),
                ),
                patch(
                    "mlb_backtest.v52_acquire._remaining_populations",
                    side_effect=populations,
                ),
                patch(
                    "mlb_backtest.v52_acquire._run_remaining_wave",
                    side_effect=mixed_wave,
                ),
                patch(
                    "mlb_backtest.v52_acquire._materialize_remaining_split",
                    side_effect=materialize,
                ),
                patch(
                    "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                    side_effect=checkpoint,
                ),
            )
            with ExitStack() as stack:
                for context in patches:
                    stack.enter_context(context)
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "during mixed split repair"
                ):
                    _run_remaining_stage(
                        request,
                        _AcquisitionPlan(
                            "a" * 64,
                            ("1" * 64, root_batch.batch_id, "0" * 64),
                            ("PROMOTED", "FETCH_REQUIRED", "FETCH_REQUIRED"),
                        ),
                        store,
                        latest=initial,
                        **shared,
                    )
                stale_checkpoint = checkpoints[-1]
                with self.assertRaisesRegex(OperationalBlock, "quarantined"):
                    _run_remaining_stage(
                        request,
                        _AcquisitionPlan(
                            "a" * 64,
                            ("1" * 64, root_batch.batch_id, "0" * 64),
                            ("PROMOTED", "FETCH_REQUIRED", "FETCH_REQUIRED"),
                        ),
                        store,
                        latest=stale_checkpoint,
                        **shared,
                    )
                mixed_checkpoint = checkpoints[-1]
                self.assertEqual(mixed_checkpoint.split_query_ids, (split_parent_id,))
                self.assertEqual(
                    mixed_checkpoint.quarantined_query_ids, (quarantine_id,)
                )
                before = {
                    path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                }
                checkpoint_count = len(checkpoints)
                with self.assertRaisesRegex(OperationalBlock, "quarantined"):
                    _run_remaining_stage(
                        request,
                        _AcquisitionPlan(
                            "a" * 64,
                            ("1" * 64, root_batch.batch_id, "0" * 64),
                            ("PROMOTED", "FETCH_REQUIRED", "FETCH_REQUIRED"),
                        ),
                        store,
                        latest=mixed_checkpoint,
                        **shared,
                    )
                after = {
                    path.relative_to(root): (path.read_bytes(), path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                }
            self.assertEqual(materialize_calls, 2)
            self.assertEqual(wave_calls, 1)
            self.assertEqual(len(checkpoints), checkpoint_count)
            self.assertEqual(after, before)

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

    def test_participant_success_before_checkpoint_never_rewrites_or_refetches(
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
                path.relative_to(store.root): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
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
                stage="PARTICIPANT_DERIVED",
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
                path.relative_to(store.root): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
                for path in store.root.rglob("*")
                if path.is_file()
            }
        self.assertEqual(after, before)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(
            (result.fetched, result.resumed, result.network_attempts), (0, 1, 0)
        )

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
            timezone_root = (
                root / "audit/v5_2_qd/historical/acquisition/timezone_reviews"
            )
            self.assertFalse(timezone_root.exists())
        self.assertEqual(actual, summary)
        self.assertEqual(state["phase"], "ACQUIRING")

    def test_full_run_requires_manual_gate_then_runs_exact_participant_stage(
        self,
    ) -> None:
        from mlb_backtest import v52_game_evidence_inventory as game_inventory_module
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _AcquisitionPlan,
            _RunnerGuard,
            _StageResult,
            _real_run_stages,
        )

        self.assertTrue(callable(game_inventory_module.publish_game_evidence_inventory))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = dataclasses.replace(
                self._request(plan_only=False, resume=True), root=root, stop_after=None
            )
            plan = _AcquisitionPlan("a" * 64, ("b" * 64,), ("FETCH_REQUIRED",))
            queries = (
                _query(),
                dataclasses.replace(
                    _query(),
                    parameters={"season": 2023, "sportId": 1},
                    requested_range={"year": 2023},
                ),
                dataclasses.replace(
                    _query(),
                    parameters={"season": 2024, "sportId": 1},
                    requested_range={"year": 2024},
                ),
            )
            static = SimpleNamespace(batch_id="c" * 64, queries=(queries[0],))
            reference = SimpleNamespace(
                batch_id="d" * 64,
                derived_from_sha256="1" * 64,
                queries=(queries[1],),
            )
            participant = SimpleNamespace(
                batch_id="f" * 64,
                derived_from_sha256="2" * 64,
                queries=(queries[2],),
            )
            prefix_ids = tuple(sorted(logical_query_id(query) for query in queries[:2]))
            all_ids = tuple(sorted(logical_query_id(query) for query in queries))
            latest = SimpleNamespace(
                receipt_sha256="e" * 64,
                stage="REFERENCE_INPUTS",
                query_batch_ids=(static.batch_id, reference.batch_id),
                completed_query_ids=prefix_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            participant_checkpoint = SimpleNamespace(
                receipt_sha256="7" * 64,
                stage="PARTICIPANT_DERIVED",
                query_batch_ids=(
                    static.batch_id,
                    reference.batch_id,
                    participant.batch_id,
                ),
                completed_query_ids=all_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
            )
            schedule = SimpleNamespace(
                path=root
                / "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
                sha256="1" * 64,
            )
            game = SimpleNamespace(
                path=root
                / "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json",
                sha256="2" * 64,
            )
            candidate_path = (
                root
                / "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json"
            )
            expected_timezone_evidence = (
                schedule.path,
                root
                / "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
                root
                / "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
                candidate_path,
                root / "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
                root / "config/v5_2_qd/venue_timezones.json",
            )
            schedule.path.parent.mkdir(parents=True, exist_ok=True)
            schedule.path.write_text("{}\n")
            candidate_path.write_text("{}\n")
            (root / "audit/v5_2_qd/historical/acquisition/timezone_reviews").mkdir()
            checkpoint_calls: list[Mapping[str, object]] = []
            published_checkpoints: list[SimpleNamespace] = []
            execute_calls: list[Mapping[str, object]] = []
            events: list[str] = []

            def checkpoint(*_args, **kwargs):
                events.append("timezone-checkpoint")
                checkpoint_calls.append(kwargs)
                evidence = tuple(kwargs["stage_evidence_receipts"])
                sealed = SimpleNamespace(
                    receipt_sha256="6" * 64,
                    stage="TIMEZONE_REVIEW",
                    query_batch_ids=tuple(kwargs["query_batch_ids"]),
                    completed_query_ids=prefix_ids,
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                    split_receipt_sha256s=(),
                    stage_evidence_receipt_paths=tuple(
                        path.relative_to(root).as_posix() for path in evidence
                    ),
                    stage_evidence_receipt_sha256s=tuple(
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in evidence
                    ),
                )
                published_checkpoints.append(sealed)
                return sealed

            def execute(*_args, **kwargs):
                events.append("participant-execute")
                execute_calls.append(kwargs)
                return _StageResult(participant_checkpoint, 1, 0, 1)

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._real_authenticate", return_value=plan
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._current_runner_guard",
                        return_value=_RunnerGuard(10, 11, root),
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._ensure_canonical_raw_parent",
                        return_value=root / "raw",
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire.V52EvidenceStore",
                        return_value=object(),
                    )
                )
                load_latest = stack.enter_context(
                    patch(
                        "mlb_backtest.execution_registry.load_latest_acquisition_checkpoint",
                        return_value=latest,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.execution_registry.publish_acquisition_checkpoint",
                        side_effect=checkpoint,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.build_static_batch",
                        return_value=static,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.load_query_batch",
                        side_effect=lambda _root, batch_id: {
                    static.batch_id: static,
                    reference.batch_id: reference,
                    participant.batch_id: participant,
                        }[batch_id],
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_schedule_inventory.load_verified_schedule_anchor_inventory",
                        return_value=schedule,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._validate_schedule_inventory_schema"
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.build_schedule_reference_batch",
                        return_value=reference,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._stage_populations",
                        side_effect=lambda _store, batches: (
                            (prefix_ids if len(batches) == 2 else all_ids),
                            (),
                            (),
                        ),
                    )
                )
                candidate = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_timezone_review.publish_timezone_candidate",
                        return_value=candidate_path,
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_timezone_review.load_verified_timezone_gate"
                    )
                )
                load_game = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_game_evidence_inventory.load_verified_game_evidence_inventory",
                        return_value=game,
                    )
                )
                publish_game = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_game_evidence_inventory.publish_game_evidence_inventory",
                        side_effect=lambda _root: (
                            events.append("game-inventory") or game
                        ),
                    )
                )
                build_participant = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_query_plan.build_participant_derived_batch",
                        side_effect=lambda _root: (
                            events.append("participant-batch") or participant
                        ),
                    )
                )
                publish_batch = stack.enter_context(
                    patch("mlb_backtest.v52_query_plan.publish_query_batch")
                )
                execute_stage = stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._execute_stage", side_effect=execute
                    )
                )
                stack.enter_context(
                    patch(
                        "mlb_backtest.v52_acquire._run_remaining_stage",
                        side_effect=OperationalBlock("REMAINING"),
                    )
                )

                with self.assertRaisesRegex(OperationalBlock, "timezone|review|seal"):
                    _real_run_stages(request, plan)
                self.assertEqual(candidate.call_count, 1)
                self.assertFalse(checkpoint_calls)
                self.assertFalse(execute_calls)

                gate_paths = (
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
                    "config/v5_2_qd/venue_timezones.json",
                )
                for relative in gate_paths:
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}\n")

                publish_game.side_effect = KeyboardInterrupt(
                    "after timezone checkpoint"
                )
                with self.assertRaises(KeyboardInterrupt):
                    _real_run_stages(request, plan)
                self.assertEqual(len(checkpoint_calls), 1)
                load_latest.return_value = published_checkpoints[-1]

                original_schedule = schedule.path.read_bytes()
                blocked_game_calls = publish_game.call_count
                blocked_execute_calls = execute_stage.call_count
                publish_game.side_effect = AssertionError(
                    "unsafe checkpoint reached game inventory"
                )

                symlink_target = root / "schedule-symlink-target.json"
                symlink_target.write_bytes(original_schedule)
                schedule.path.unlink()
                schedule.path.symlink_to(symlink_target)
                with self.assertRaisesRegex(TrustIntegrityError, "timezone checkpoint"):
                    _real_run_stages(request, plan)
                self.assertEqual(publish_game.call_count, blocked_game_calls)
                self.assertEqual(execute_stage.call_count, blocked_execute_calls)
                schedule.path.unlink()
                schedule.path.write_bytes(original_schedule)

                hardlink_target = root / "schedule-hardlink-target.json"
                hardlink_target.write_bytes(original_schedule)
                schedule.path.unlink()
                os.link(hardlink_target, schedule.path)
                with self.assertRaisesRegex(TrustIntegrityError, "timezone checkpoint"):
                    _real_run_stages(request, plan)
                self.assertEqual(publish_game.call_count, blocked_game_calls)
                self.assertEqual(execute_stage.call_count, blocked_execute_calls)
                schedule.path.unlink()
                hardlink_target.unlink()
                schedule.path.write_bytes(original_schedule)

                real_read = os.read
                displaced = root / "schedule-displaced.json"
                rebound = False

                def rebind_during_read(descriptor, size):
                    nonlocal rebound
                    chunk = real_read(descriptor, size)
                    if chunk and not rebound:
                        schedule.path.replace(displaced)
                        schedule.path.write_bytes(original_schedule)
                        rebound = True
                    return chunk

                with (
                    patch(
                        "mlb_backtest.v52_acquire.os.read",
                        side_effect=rebind_during_read,
                    ),
                    self.assertRaisesRegex(TrustIntegrityError, "timezone checkpoint"),
                ):
                    _real_run_stages(request, plan)
                self.assertTrue(rebound)
                self.assertEqual(publish_game.call_count, blocked_game_calls)
                self.assertEqual(execute_stage.call_count, blocked_execute_calls)
                displaced.unlink()

                publish_game.side_effect = lambda _root: (
                    events.append("game-inventory") or game
                )
                build_participant.side_effect = KeyboardInterrupt(
                    "after game inventory"
                )
                with self.assertRaises(KeyboardInterrupt):
                    _real_run_stages(request, plan)

                build_participant.side_effect = lambda _root: (
                    events.append("participant-batch") or participant
                )
                with self.assertRaisesRegex(OperationalBlock, "REMAINING"):
                    _real_run_stages(request, plan)

                published_game_calls = publish_game.call_count
                published_batch_calls = publish_batch.call_count
                executed_calls = execute_stage.call_count
                load_latest.return_value = participant_checkpoint
                with self.assertRaisesRegex(OperationalBlock, "REMAINING"):
                    _real_run_stages(request, plan)
                self.assertEqual(publish_game.call_count, published_game_calls)
                self.assertEqual(publish_batch.call_count, published_batch_calls)
                self.assertEqual(execute_stage.call_count, executed_calls)
                load_game.assert_called_once_with(root)

            self.assertEqual(checkpoint_calls[0]["stage"], "TIMEZONE_REVIEW")
            self.assertEqual(
                checkpoint_calls[0]["query_batch_ids"],
                (static.batch_id, reference.batch_id),
            )
            self.assertEqual(
                checkpoint_calls[0]["stage_evidence_receipts"],
                expected_timezone_evidence,
            )
            self.assertEqual(execute_calls[0]["stage"], "PARTICIPANT_DERIVED")
            self.assertEqual(
                execute_calls[0]["stage_evidence"],
                tuple(sorted((*expected_timezone_evidence, game.path), key=os.fspath)),
            )
            self.assertLess(
                events.index("timezone-checkpoint"), events.index("game-inventory")
            )
            self.assertLess(
                events.index("game-inventory"), events.index("participant-batch")
            )
            self.assertLess(
                events.index("participant-batch"), events.index("participant-execute")
            )

    def test_timezone_gate_classifies_missing_as_operational_and_tamper_as_trust(
        self,
    ) -> None:
        from mlb_backtest.v52_source_contracts import canonical_json_bytes
        from mlb_backtest.v52_acquire import (
            OperationalBlock,
            TrustIntegrityError,
            _load_required_timezone_gate,
        )
        from tests.test_v52_timezone_review import _fixed_verified_gate_fixture

        old_run_spec = self._request(plan_only=True).run_spec

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixed_verified_gate_fixture(root)
            gate = _load_required_timezone_gate(root, old_run_spec)
            self.assertEqual(len(gate.reviews), 2)

            review = (
                root
                / "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json"
            )
            original = review.read_bytes()
            review.write_bytes(review.read_bytes() + b" ")
            with self.assertRaisesRegex(TrustIntegrityError, "timezone gate"):
                _load_required_timezone_gate(root, old_run_spec)

            binding_invalid = json.loads(original)
            binding_invalid["reviewer_identity"] = "forged-reviewer"
            review.write_bytes(canonical_json_bytes(binding_invalid) + b"\n")
            with self.assertRaisesRegex(TrustIntegrityError, "timezone gate"):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            review_parent = (
                root / "audit/v5_2_qd/historical/acquisition/timezone_reviews"
            )
            review_parent.mkdir(parents=True)
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("missing leaf reached loader"),
                ),
                self.assertRaisesRegex(OperationalBlock, "review|seal"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            acquisition.mkdir(parents=True)
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("missing ancestor reached loader"),
                ),
                self.assertRaisesRegex(OperationalBlock, "review|seal"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            review_parent = acquisition / "timezone_reviews"
            review_parent.mkdir(parents=True)
            for path in (
                review_parent / "coverage.json",
                review_parent / "semantic.json",
                acquisition / "venue_timezone_seal.json",
            ):
                path.write_text("{}\n")
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("missing config parent reached loader"),
                ),
                self.assertRaisesRegex(OperationalBlock, "review|seal"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            acquisition.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            for leaf in ("coverage.json", "semantic.json"):
                (outside / leaf).write_text("{}\n")
            (acquisition / "timezone_reviews").symlink_to(
                outside, target_is_directory=True
            )
            (acquisition / "venue_timezone_seal.json").write_text("{}\n")
            mapping = root / "config/v5_2_qd/venue_timezones.json"
            mapping.parent.mkdir(parents=True)
            mapping.write_text("{}\n")
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("symlinked ancestor reached loader"),
                ),
                self.assertRaisesRegex(TrustIntegrityError, "ancestor|identity|path"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            review_parent = acquisition / "timezone_reviews"
            review_parent.mkdir(parents=True)
            for path in (
                review_parent / "coverage.json",
                review_parent / "semantic.json",
                acquisition / "venue_timezone_seal.json",
            ):
                path.write_text("{}\n")
            outside = root / "outside-config"
            (outside / "v5_2_qd").mkdir(parents=True)
            (outside / "v5_2_qd/venue_timezones.json").write_text("{}\n")
            (root / "config").symlink_to(outside, target_is_directory=True)
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("symlinked config reached loader"),
                ),
                self.assertRaisesRegex(TrustIntegrityError, "ancestor|identity|path"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            review_parent = (
                root / "audit/v5_2_qd/historical/acquisition/timezone_reviews"
            )
            review_parent.mkdir(parents=True)
            outside = root / "coverage-source.json"
            outside.write_text("{}\n")
            os.link(outside, review_parent / "coverage.json")
            with (
                patch(
                    "mlb_backtest.v52_timezone_review.load_verified_timezone_gate",
                    side_effect=AssertionError("hardlink reached loader"),
                ),
                self.assertRaisesRegex(TrustIntegrityError, "identity|hardlink|path"),
            ):
                _load_required_timezone_gate(root, old_run_spec)

    def test_resumed_timezone_checkpoint_requires_exact_boundary_before_inventory(
        self,
    ) -> None:
        from mlb_backtest.execution_registry import AcquisitionCheckpoint
        from mlb_backtest.v52_acquire import (
            TrustIntegrityError,
            _validate_resumed_timezone_checkpoint,
        )

        old_run_spec = self._request(plan_only=True).run_spec

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence = tuple(
                root / path
                for path in (
                    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
                    "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
                    "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
                    "config/v5_2_qd/venue_timezones.json",
                )
            )
            for index, path in enumerate(evidence):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"artifact-{index}\n".encode())
            relative_evidence = tuple(
                path.relative_to(root).as_posix() for path in evidence
            )
            batches = ("a" * 64, "b" * 64)
            completed = ("c" * 64, "d" * 64)
            checkpoint = AcquisitionCheckpoint(
                sequence=3,
                receipt_sha256="1" * 64,
                prior_checkpoint_sha256="2" * 64,
                stage="TIMEZONE_REVIEW",
                recorded_at_utc="2026-07-16T12:00:00.000000Z",
                git_head="3" * 40,
                execution_receipt_sha256="4" * 64,
                query_batch_ids=batches,
                query_batch_sha256s=("5" * 64, "6" * 64),
                query_evidence_receipt_sha256s=(),
                stage_evidence_receipt_sha256s=tuple(
                    hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence
                ),
                split_receipt_sha256s=(),
                completed_query_ids=completed,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                stage_evidence_receipt_paths=relative_evidence,
            )
            _validate_resumed_timezone_checkpoint(
                checkpoint,
                root=root,
                batch_ids=batches,
                completed_query_ids=completed,
                stage_evidence=evidence,
                run_spec=old_run_spec,
            )

            cases = {
                "extra batch": {"query_batch_ids": (*batches, "e" * 64)},
                "missing batch": {"query_batch_ids": batches[:1]},
                "extra completed": {"completed_query_ids": (*completed, "e" * 64)},
                "missing completed": {"completed_query_ids": completed[:1]},
                "pending": {"pending_query_ids": ("e" * 64,)},
                "quarantine": {"quarantined_query_ids": ("e" * 64,)},
                "split": {"split_query_ids": ("e" * 64,)},
                "split receipt": {"split_receipt_sha256s": ("e" * 64,)},
                "evidence path": {
                    "stage_evidence_receipt_paths": tuple(reversed(relative_evidence))
                },
                "evidence hash": {"stage_evidence_receipt_sha256s": ("f" * 64,) * 6},
            }
            for label, mutation in cases.items():
                forged = dataclasses.replace(checkpoint, **mutation)
                with (
                    self.subTest(label=label),
                    self.assertRaisesRegex(TrustIntegrityError, "timezone checkpoint"),
                ):
                    _validate_resumed_timezone_checkpoint(
                        forged,
                        root=root,
                        batch_ids=batches,
                        completed_query_ids=completed,
                        stage_evidence=evidence,
                        run_spec=old_run_spec,
                    )

    def test_shared_disk_budget_prevents_cross_pool_overcommit_and_releases(
        self,
    ) -> None:
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

    def test_disk_budget_stays_reserved_across_retries_and_releases_on_errors(
        self,
    ) -> None:
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
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
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

    def test_exhausted_dangling_reservations_checkpoint_and_report_pending(
        self,
    ) -> None:
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
                    network_gate=lambda *_args, **_kwargs: self.fail("exhausted gated"),
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
            '"quarantined":0,"query_batch_sha256s":["' + "b" * 64 + '"],"resumed":0}\n'
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

    def test_cli_normalizes_relative_superseding_contract_before_authentication(
        self,
    ) -> None:
        from mlb_backtest.v52_acquire import (
            _AcquisitionPlan,
            _REAL_OPS,
            _validate_request_contract,
            main,
        )

        source_run_spec = Path(
            "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
        )
        source_contract = Path(
            "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
        )
        legacy_root = ROOT / "data/v5_1_bt/raw"
        created_legacy_root = not legacy_root.exists()
        legacy_root.mkdir(parents=True, exist_ok=True)
        previous_cwd = Path.cwd()

        def authenticate(request):
            _root, _contract, contract_contents = _validate_request_contract(request)
            return _AcquisitionPlan(
                contract_sha256=hashlib.sha256(contract_contents).hexdigest(),
                query_batch_ids=("a" * 64,),
                legacy_dispositions=("PROMOTED",),
            )

        argv = [
            "--root",
            ".",
            "--run-spec",
            source_run_spec.as_posix(),
            "--acquisition-contract",
            source_contract.as_posix(),
            "--years",
            "2018:2024",
            "--mode",
            "hybrid",
            "--legacy-root",
            "data/v5_1_bt/raw",
            "--protected-inventory",
            "audit/v5_1_bt/protected_raw_inventory.json",
            "--plan-only",
        ]
        output = io.StringIO()
        try:
            os.chdir(ROOT)
            fake_ops = dataclasses.replace(_REAL_OPS, authenticate=authenticate)
            with (
                patch("mlb_backtest.v52_acquire._REAL_OPS", fake_ops),
                patch("sys.stdout", output),
            ):
                self.assertEqual(main(argv), 0)
        finally:
            os.chdir(previous_cwd)
            if created_legacy_root:
                legacy_root.rmdir()

        payload = json.loads(output.getvalue())
        expected_contract_sha256 = hashlib.sha256(
            (ROOT / source_contract).read_bytes()
        ).hexdigest()
        self.assertEqual(payload["contract_sha256"], expected_contract_sha256)
        self.assertEqual(payload["query_batch_sha256s"], ["a" * 64])

    def test_cli_defaults_recovery_run_to_recovery_contract(self) -> None:
        from mlb_backtest.protocols import (
            expected_recovery_source_run,
            parse_source_run_spec,
        )
        from mlb_backtest.v52_acquire import (
            AcquisitionSummary,
            _RECOVERY_ACQUISITION_CONTRACT_RELATIVE,
            _RECOVERY_RUN_SPEC_RELATIVE,
            main,
        )

        recovery_run = parse_source_run_spec(expected_recovery_source_run())
        summary = AcquisitionSummary(
            contract_sha256="a" * 64,
            query_batch_sha256s=("b" * 64,),
            promoted=0,
            fetched=0,
            planned_promoted=0,
            planned_fetch_required=0,
            resumed=0,
            quarantined=0,
            pending=0,
            network_attempts=0,
        )
        argv = self._cli_argv()
        argv[argv.index("--run-spec") + 1] = str(ROOT / _RECOVERY_RUN_SPEC_RELATIVE)
        with (
            patch(
                "mlb_backtest.v52_acquire._cli_exact_run_spec",
                return_value=ROOT / _RECOVERY_RUN_SPEC_RELATIVE,
            ),
            patch(
                "mlb_backtest.v52_acquire.load_source_run_spec",
                return_value=recovery_run,
            ),
            patch(
                "mlb_backtest.v52_acquire.acquire_sources",
                return_value=summary,
            ) as acquire,
        ):
            self.assertEqual(main(argv), 0)
        self.assertEqual(
            acquire.call_args.kwargs["acquisition_contract_path"],
            ROOT / _RECOVERY_ACQUISITION_CONTRACT_RELATIVE,
        )

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
