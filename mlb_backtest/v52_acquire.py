"""Resumable single-query acquisition with frozen retry semantics."""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ContextManager, Literal, TypeVar
from urllib.parse import urlparse

from .protocols import RunSpec, SourceRunSpec, load_run_spec, load_source_run_spec
from .v52_evidence import (
    AttemptBudgetSnapshot,
    AttemptCapture,
    AttemptRetryState,
    LogicalQuery,
    PublishedEvidence,
    V52EvidenceStore,
    logical_query_id,
)
from .v52_http import (
    DEFAULT_TIMEOUT_SECONDS,
    FetchLimits,
    RetryPolicy,
    deterministic_jitter,
    fetch_attempt,
    fetch_limits,
    parse_retry_after,
)
from .v52_source_contracts import canonical_json_bytes


_TRANSIENT_OUTCOMES = frozenset({"TRANSIENT_HTTP", "TRANSIENT_NETWORK"})
_TERMINAL_OUTCOMES = frozenset({"PERMANENT_HTTP", "PERMANENT_NETWORK", "QUARANTINED"})
_SPLIT_REASONS = frozenset({"ROW_CAP", "SIZE_LIMIT", "TIMEOUT"})
_FROZEN_DELAYS = (1, 2, 4, 8, 16)
_FROZEN_JITTER_FRACTION = 0.25
_RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition.json"
)
_LEGACY_ROOT_RELATIVE = Path("data/v5_1_bt/raw")
_PROTECTED_INVENTORY_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
_SCHEDULE_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
_SCHEDULE_INVENTORY_SCHEMA_RELATIVE = Path(
    "schemas/v5_2_schedule_anchor_inventory.schema.json"
)
_TIMEZONE_STAGE_EVIDENCE_RELATIVES = (
    _SCHEDULE_INVENTORY_RELATIVE,
    Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json"),
    Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json"),
    Path("audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json"),
    Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"),
    Path("config/v5_2_qd/venue_timezones.json"),
)
_TIMEZONE_STAGE_EVIDENCE_LIMITS = (
    64 * 1024 * 1024,
    1024 * 1024,
    1024 * 1024,
    64 * 1024 * 1024,
    1024 * 1024,
    8 * 1024 * 1024,
)
_DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
_PLAN_RELATIVE = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
_RUNNER_LOCK_RELATIVE = Path(
    "audit/v5_2_qd/historical/execution/.acquisition-runner.lock"
)
_MINIMUM_RESERVE_BYTES = 20 * 1024**3
_EXPECTED_YEARS = range(2018, 2025)
_HOST_WORKERS = {
    "baseballsavant.mlb.com": 1,
    "statsapi.mlb.com": 2,
}
_T = TypeVar("_T")


@dataclass(frozen=True)
class _RunnerGuard:
    descriptor: int
    parent_descriptor: int
    root: Path


class _DiskBudget:
    def __init__(
        self,
        request: _AcquisitionRequest,
        *,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self._root = Path(os.path.abspath(os.fspath(request.root)))
        self._reserve_bytes = request.reserve_bytes
        self._free_bytes = (
            (lambda root: shutil.disk_usage(root).free)
            if free_bytes is None
            else free_bytes
        )
        self._lock = threading.Lock()
        self._reserved_bytes = 0
        self._peak_reserved_bytes = 0

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    @property
    def peak_reserved_bytes(self) -> int:
        with self._lock:
            return self._peak_reserved_bytes

    def _current_free_locked(self) -> int:
        try:
            free = self._free_bytes(self._root)
        except OSError as exc:
            raise OperationalBlock("free-space reserve is unavailable") from exc
        if isinstance(free, bool) or not isinstance(free, int) or free < 0:
            raise TrustIntegrityError("free-space measurement differs")
        return free

    @contextmanager
    def reserve(self, query: LogicalQuery) -> Iterator[None]:
        amount = fetch_limits(query).max_bytes
        with self._lock:
            free = self._current_free_locked()
            required = self._reserve_bytes + self._reserved_bytes + amount
            if free < required:
                raise OperationalBlock(
                    "free-space reserve would be violated by concurrent fetch"
                )
            self._reserved_bytes += amount
            self._peak_reserved_bytes = max(
                self._peak_reserved_bytes,
                self._reserved_bytes,
            )
        try:
            yield
        finally:
            with self._lock:
                self._reserved_bytes -= amount
                if self._reserved_bytes < 0:
                    raise AssertionError("disk reservation underflow")

    def recheck(self) -> None:
        with self._lock:
            free = self._current_free_locked()
            if free < self._reserve_bytes + self._reserved_bytes:
                raise OperationalBlock(
                    "free-space reserve would be violated by active fetches"
                )


def _integrity_boundary(label: str, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustIntegrityError(f"{label} integrity verification failed") from exc


_ACTIVE_RUNNER: ContextVar[_RunnerGuard | None] = ContextVar(
    "v52_active_acquisition_runner",
    default=None,
)


@dataclass(frozen=True)
class SplitRequired(Exception):
    reason: Literal["ROW_CAP", "SIZE_LIMIT", "TIMEOUT"]
    exhausted_attempt_number: int

    def __post_init__(self) -> None:
        if self.reason not in _SPLIT_REASONS:
            raise ValueError("split reason must be ROW_CAP, SIZE_LIMIT, or TIMEOUT")
        if (
            isinstance(self.exhausted_attempt_number, bool)
            or not isinstance(self.exhausted_attempt_number, int)
            or self.exhausted_attempt_number <= 0
        ):
            raise ValueError("exhausted attempt number must be positive")
        object.__setattr__(
            self,
            "args",
            (self.reason, self.exhausted_attempt_number),
        )


def _split_required_setattr(instance: SplitRequired, name: str, value: object) -> None:
    # Exception machinery must attach traceback/context while the outcome fields
    # remain frozen after dataclass initialization.
    if name in {"reason", "exhausted_attempt_number"} and hasattr(instance, name):
        raise FrozenInstanceError(f"cannot assign to field {name!r}")
    object.__setattr__(instance, name, value)


SplitRequired.__setattr__ = _split_required_setattr  # type: ignore[method-assign]


class TerminalAcquisitionFailure(RuntimeError):
    def __init__(self, classification: str, attempt_number: int | None) -> None:
        if not isinstance(classification, str) or not classification:
            raise ValueError("terminal classification must be nonempty text")
        if attempt_number is not None and (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number <= 0
        ):
            raise ValueError("terminal attempt number must be positive or null")
        self.classification = classification
        self.attempt_number = attempt_number
        suffix = "" if attempt_number is None else f" at attempt {attempt_number}"
        super().__init__(f"terminal acquisition failure: {classification}{suffix}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _terminal_progress(progress: object) -> PublishedEvidence | Exception | None:
    published = getattr(progress, "published_evidence", None)
    status = getattr(progress, "status", None)
    receipts = getattr(progress, "attempt_receipts", ())
    if status == "SUCCESS":
        if not isinstance(published, PublishedEvidence):
            raise ValueError("successful progress has no verified evidence")
        return published
    if not isinstance(receipts, tuple):
        raise ValueError("verified attempt history differs")
    split_receipts = [
        item for item in receipts if item.get("outcome") == "SPLIT_REQUIRED"
    ]
    if split_receipts:
        last = split_receipts[-1]
        reason = last.get("error_classification")
        attempt_number = last.get("attempt_number")
        if reason not in _SPLIT_REASONS or type(attempt_number) is not int:
            raise ValueError("split progress binding differs")
        return SplitRequired(reason, attempt_number)  # type: ignore[arg-type]
    terminal = [item for item in receipts if item.get("outcome") in _TERMINAL_OUTCOMES]
    if status == "QUARANTINED" or terminal:
        if not terminal:
            raise ValueError("quarantined progress has no terminal attempt")
        last = terminal[-1]
        classification = last.get("error_classification")
        attempt_number = last.get("attempt_number")
        if not isinstance(classification, str) or type(attempt_number) is not int:
            raise ValueError("terminal progress binding differs")
        return TerminalAcquisitionFailure(classification, attempt_number)
    return None


def _is_multiday_statcast(query: LogicalQuery) -> bool:
    if (
        query.source != "baseball_savant_statcast"
        or query.grain != "pitch"
        or query.endpoint != "https://baseballsavant.mlb.com/statcast_search/csv"
    ):
        return False
    first_raw = query.requested_range.get("start_date")
    last_raw = query.requested_range.get("end_date")
    if not isinstance(first_raw, str) or not isinstance(last_raw, str):
        raise ValueError("Statcast query range is missing")
    first = date.fromisoformat(first_raw)
    last = date.fromisoformat(last_raw)
    if first > last:
        raise ValueError("Statcast query range is reversed")
    return first < last


def _quarantine(
    store: V52EvidenceStore,
    query: LogicalQuery,
    attempt_number: int,
    capture: AttemptCapture,
    classification: str,
) -> None:
    if capture.body is None:
        store.quarantine_bodyless_attempt(
            query,
            attempt_number,
            capture,
            error_classification=classification,
        )
    else:
        store.quarantine_attempt(
            query,
            attempt_number,
            capture,
            error_classification=classification,
        )


def _persist_split(
    store: V52EvidenceStore,
    query: LogicalQuery,
    attempt_number: int,
    capture: AttemptCapture,
    reason: Literal["ROW_CAP", "SIZE_LIMIT", "TIMEOUT"],
) -> SplitRequired:
    store.persist_split_attempt(
        query,
        attempt_number,
        capture,
        reason=reason,
    )
    return SplitRequired(reason, attempt_number)


def _unlocked_attempt_budget(
    query: LogicalQuery,
    progress: object,
) -> AttemptBudgetSnapshot:
    receipts = getattr(progress, "attempt_receipts", None)
    pending = getattr(progress, "pending_attempt_numbers", None)
    if not isinstance(receipts, tuple) or not isinstance(pending, tuple):
        raise ValueError("verified attempt budget snapshot differs")
    receipt_numbers = tuple(item.get("attempt_number") for item in receipts)
    if any(type(number) is not int for number in receipt_numbers) or any(
        type(number) is not int for number in pending
    ):
        raise ValueError("verified attempt budget numbers differ")
    reservations = tuple(sorted((*receipt_numbers, *pending)))
    if len(reservations) != len(set(reservations)):
        raise ValueError("verified attempt budget reservations overlap")
    query_id = logical_query_id(query)
    bindings = tuple(
        (
            int(item["attempt_number"]),
            (
                f"queries/{query.source}/{query_id}/attempts/"
                f"{int(item['attempt_number']):06d}.json"
            ),
            hashlib.sha256(canonical_json_bytes(dict(item)) + b"\n").hexdigest(),
        )
        for item in receipts
    )
    latest = receipts[-1] if receipts else None
    latest_receipt = None
    if latest is not None:
        headers = latest.get("response_headers")
        if not isinstance(headers, Mapping):
            raise ValueError("verified latest attempt headers differ")
        outcome = latest.get("outcome")
        classification = latest.get("error_classification")
        ended_at_utc = latest.get("ended_at_utc")
        attempt_number = latest.get("attempt_number")
        if (
            not isinstance(outcome, str)
            or classification is not None
            and not isinstance(classification, str)
            or not isinstance(ended_at_utc, str)
            or type(attempt_number) is not int
        ):
            raise ValueError("verified latest attempt receipt differs")
        retryable = outcome in _TRANSIENT_OUTCOMES
        retry_ordinal = sum(
            item.get("outcome") in _TRANSIENT_OUTCOMES for item in receipts
        )
        retry_after = headers.get("Retry-After")
        if retry_after is not None and not isinstance(retry_after, str):
            raise ValueError("verified latest Retry-After differs")
        latest_receipt = AttemptRetryState(
            attempt_number=attempt_number,
            retry_ordinal=retry_ordinal if retryable else None,
            outcome=outcome,
            error_classification=classification,
            ended_at_utc=ended_at_utc,
            retry_after=retry_after,
        )
    return AttemptBudgetSnapshot(
        reservation_numbers=reservations,
        receipt_bindings=bindings,
        latest_receipt=latest_receipt,
    )


def _parsed_persisted_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("persisted attempt end timestamp differs") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("persisted attempt end timestamp differs")
    return parsed


def _remaining_retry_delay(
    query: LogicalQuery,
    latest: AttemptRetryState | None,
    *,
    policy: RetryPolicy,
    clock: Callable[[], datetime],
) -> float:
    if latest is None or latest.outcome not in _TRANSIENT_OUTCOMES:
        return 0.0
    ordinal = latest.retry_ordinal
    if type(ordinal) is not int or ordinal <= 0 or ordinal > len(policy.base_delays):
        raise ValueError("persisted retry ordinal differs from the frozen policy")
    attempt_number = latest.attempt_number
    if type(attempt_number) is not int or attempt_number <= 0:
        raise ValueError("persisted attempt number differs")
    ended = _parsed_persisted_utc(latest.ended_at_utc)
    retry_after = parse_retry_after(latest.retry_after, now_utc=ended)
    base_delay = policy.base_delays[ordinal - 1]
    jittered = deterministic_jitter(
        logical_query_id(query),
        attempt_number,
        base_delay,
        policy.jitter_fraction,
    )
    deadline = ended + timedelta(seconds=max(jittered, retry_after or 0.0))
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return max(
        0.0,
        (deadline - now.astimezone(timezone.utc)).total_seconds(),
    )


def acquire_with_retries(
    store: V52EvidenceStore,
    query: LogicalQuery,
    query_parser: Callable[[bytes, LogicalQuery], Mapping[str, object]],
    *,
    policy: RetryPolicy,
    limits: FetchLimits,
    before_network: Callable[[LogicalQuery], None],
    fetcher: Callable[..., AttemptCapture] = fetch_attempt,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = _utc_now,
) -> PublishedEvidence:
    """Acquire one query without exceeding its append-only six-attempt budget."""
    if not isinstance(store, V52EvidenceStore):
        raise TypeError("store must be a V52EvidenceStore")
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if not callable(query_parser):
        raise TypeError("query_parser must be callable")
    if not isinstance(policy, RetryPolicy):
        raise TypeError("policy must be a RetryPolicy")
    if (
        policy.base_delays != _FROZEN_DELAYS
        or policy.jitter_fraction != _FROZEN_JITTER_FRACTION
    ):
        raise ValueError("retry policy differs from the frozen six-attempt policy")
    if policy.timeout != DEFAULT_TIMEOUT_SECONDS:
        raise ValueError(
            "retry policy request timeout differs from the frozen contract"
        )
    if not isinstance(limits, FetchLimits) or limits != fetch_limits(
        query, policy=policy
    ):
        raise ValueError("fetch limits differ from the frozen query contract")
    for callback, label in (
        (before_network, "before_network"),
        (fetcher, "fetcher"),
        (sleeper, "sleeper"),
        (clock, "clock"),
    ):
        if not callable(callback):
            raise TypeError(f"{label} must be callable")

    maximum_attempts = len(policy.base_delays) + 1
    while True:
        progress = store.read_verified_query_progress(query)
        terminal = _terminal_progress(progress)
        if isinstance(terminal, PublishedEvidence):
            return terminal
        if isinstance(terminal, Exception):
            raise terminal
        receipts = progress.attempt_receipts
        unlocked_budget = _unlocked_attempt_budget(query, progress)
        transient_count = sum(
            item.get("outcome") in _TRANSIENT_OUTCOMES for item in receipts
        )
        if len(unlocked_budget.reservation_numbers) >= maximum_attempts:
            raise TerminalAcquisitionFailure("RETRY_BUDGET_ALREADY_EXHAUSTED", None)
        remaining_delay = _remaining_retry_delay(
            query,
            unlocked_budget.latest_receipt,
            policy=policy,
            clock=clock,
        )
        if remaining_delay > 0:
            sleeper(remaining_delay)
            continue

        before_network(query)
        retry_recorded = False
        with store.acquire_query(query) as lease:
            recovered = store.recover_failure_attempt(query)
            if recovered is not None:
                if recovered.outcome == "SPLIT_REQUIRED":
                    raise SplitRequired(
                        recovered.error_classification,  # type: ignore[arg-type]
                        recovered.attempt_number,
                    )
                raise TerminalAcquisitionFailure(
                    recovered.error_classification,
                    recovered.attempt_number,
                )
            published = store.load_success(query)
            if published is not None:
                return published
            if store.read_leased_attempt_budget(lease) != unlocked_budget:
                continue

            attempt_number = store.next_attempt_number(lease)
            capture = fetcher(
                query,
                timeout=limits.timeout,
                max_bytes=limits.max_bytes,
            )
            if not isinstance(capture, AttemptCapture):
                raise TypeError("fetcher must return an AttemptCapture")
            classification = capture.error_classification

            if classification in _TRANSIENT_OUTCOMES:
                transient_count += 1
                if attempt_number == maximum_attempts:
                    if capture.timed_out and _is_multiday_statcast(query):
                        raise _persist_split(
                            store,
                            query,
                            attempt_number,
                            capture,
                            "TIMEOUT",
                        )
                    terminal_classification = (
                        "TIMEOUT" if capture.timed_out else classification
                    )
                    terminal_capture = (
                        capture
                        if terminal_classification == classification
                        else replace(capture, error_classification=classification)
                    )
                    _quarantine(
                        store,
                        query,
                        attempt_number,
                        terminal_capture,
                        terminal_classification,
                    )
                    raise TerminalAcquisitionFailure(
                        terminal_classification,
                        attempt_number,
                    )
                store.record_attempt(
                    query,
                    attempt_number,
                    capture,
                    outcome=classification,
                )
                retry_recorded = True

            elif classification == "MAX_BYTES_EXCEEDED":
                reason: Literal["SIZE_LIMIT"] = "SIZE_LIMIT"
                classified_capture = replace(
                    capture,
                    error_classification=reason,
                )
                if _is_multiday_statcast(query):
                    raise _persist_split(
                        store,
                        query,
                        attempt_number,
                        classified_capture,
                        reason,
                    )
                _quarantine(
                    store,
                    query,
                    attempt_number,
                    classified_capture,
                    reason,
                )
                raise TerminalAcquisitionFailure(reason, attempt_number)

            elif classification is not None:
                _quarantine(
                    store,
                    query,
                    attempt_number,
                    capture,
                    classification,
                )
                raise TerminalAcquisitionFailure(classification, attempt_number)

            else:
                if capture.body is None:
                    raise ValueError("successful transport capture has no body bytes")
                try:
                    semantics = query_parser(capture.body, query)
                except ValueError:
                    _quarantine(
                        store,
                        query,
                        attempt_number,
                        capture,
                        "SEMANTIC_INVALID",
                    )
                    raise TerminalAcquisitionFailure(
                        "SEMANTIC_INVALID", attempt_number
                    ) from None
                if not isinstance(semantics, Mapping):
                    raise TypeError("query_parser must return a mapping")
                if semantics.get("split_disposition") == "SPLIT_REQUIRED":
                    classified_capture = replace(
                        capture,
                        error_classification="ROW_CAP",
                    )
                    if _is_multiday_statcast(query):
                        raise _persist_split(
                            store,
                            query,
                            attempt_number,
                            classified_capture,
                            "ROW_CAP",
                        )
                    _quarantine(
                        store,
                        query,
                        attempt_number,
                        classified_capture,
                        "ROW_CAP",
                    )
                    raise TerminalAcquisitionFailure("ROW_CAP", attempt_number)
                return store.publish_fetched(
                    query,
                    attempt_number,
                    capture,
                    semantics,
                )

        if not retry_recorded:
            raise RuntimeError("retry receipt is missing after transient attempt")


@dataclass(frozen=True)
class AcquisitionSummary:
    contract_sha256: str
    query_batch_sha256s: tuple[str, ...]
    promoted: int
    fetched: int
    planned_promoted: int
    planned_fetch_required: int
    resumed: int
    quarantined: int
    pending: int
    network_attempts: int


class OperationalBlock(RuntimeError):
    """An expected runtime condition prevented acquisition from starting."""


class TrustIntegrityError(ValueError):
    """The supplied runner authority differs from the frozen acquisition trust."""


@dataclass(frozen=True)
class _AcquisitionRequest:
    root: Path
    run_spec: RunSpec | SourceRunSpec
    years: range
    mode: str
    resume: bool
    stop_after: str | None
    legacy_root: Path
    protected_inventory_path: Path
    reserve_bytes: int
    plan_only: bool
    fetcher: Callable[..., AttemptCapture] = fetch_attempt
    acquisition_contract_path: Path | None = None


@dataclass(frozen=True)
class _AcquisitionPlan:
    contract_sha256: str
    query_batch_ids: tuple[str, ...]
    legacy_dispositions: tuple[str, ...]


@dataclass(frozen=True)
class _RuntimeState:
    phase: str | None
    has_progress: bool


def _stages_unavailable(
    _request: _AcquisitionRequest,
    _plan: _AcquisitionPlan,
) -> AcquisitionSummary:
    raise OperationalBlock("reference stages unavailable")


def _terminal_replay_unavailable(
    _request: _AcquisitionRequest,
    _plan: _AcquisitionPlan,
) -> AcquisitionSummary:
    raise TrustIntegrityError(
        "SOURCES_READY lacks a read-only terminal replay authority"
    )


@dataclass(frozen=True)
class _AcquireOps:
    authenticate: Callable[[_AcquisitionRequest], _AcquisitionPlan]
    runner_lock: Callable[[_AcquisitionRequest], ContextManager[None]]
    inspect_runtime: Callable[[_AcquisitionRequest, _AcquisitionPlan], _RuntimeState]
    begin_runtime: Callable[
        [_AcquisitionRequest, _AcquisitionPlan, _RuntimeState], None
    ]
    replay_terminal: Callable[
        [_AcquisitionRequest, _AcquisitionPlan], AcquisitionSummary
    ] = _terminal_replay_unavailable
    run_stages: Callable[
        [_AcquisitionRequest, _AcquisitionPlan], AcquisitionSummary
    ] = _stages_unavailable


def _summary_for_plan(plan: _AcquisitionPlan) -> AcquisitionSummary:
    hashes = (plan.contract_sha256, *plan.query_batch_ids)
    if (
        not plan.query_batch_ids
        or len(plan.query_batch_ids) != len(set(plan.query_batch_ids))
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        )
    ):
        raise TrustIntegrityError("planned authority must use unique SHA-256 IDs")
    permitted = {"PROMOTED", "FETCH_REQUIRED"}
    if not plan.legacy_dispositions or any(
        item not in permitted for item in plan.legacy_dispositions
    ):
        raise TrustIntegrityError("legacy plan contains a fatal disposition")
    return AcquisitionSummary(
        contract_sha256=plan.contract_sha256,
        query_batch_sha256s=plan.query_batch_ids,
        promoted=0,
        fetched=0,
        planned_promoted=plan.legacy_dispositions.count("PROMOTED"),
        planned_fetch_required=plan.legacy_dispositions.count("FETCH_REQUIRED"),
        resumed=0,
        quarantined=0,
        pending=0,
        network_attempts=0,
    )


def _run_acquisition(
    request: _AcquisitionRequest,
    ops: _AcquireOps,
) -> AcquisitionSummary:
    if not isinstance(request, _AcquisitionRequest):
        raise TypeError("request must be an _AcquisitionRequest")
    if not isinstance(ops, _AcquireOps):
        raise TypeError("ops must be an _AcquireOps")

    plan = ops.authenticate(request)
    if not isinstance(plan, _AcquisitionPlan):
        raise TypeError("authentication must return an _AcquisitionPlan")
    if request.plan_only:
        return _summary_for_plan(plan)

    def replay_terminal() -> AcquisitionSummary:
        if request.stop_after is not None:
            raise TrustIntegrityError(
                "SOURCES_READY requires terminal REMAINING replay"
            )
        summary = ops.replay_terminal(request, plan)
        if not isinstance(summary, AcquisitionSummary):
            raise TypeError("terminal replay must return an AcquisitionSummary")
        if (
            summary.promoted
            or summary.fetched
            or summary.network_attempts
            or summary.pending
            or summary.quarantined
        ):
            raise TrustIntegrityError(
                "SOURCES_READY terminal replay summary differs"
            )
        return summary

    later_phases = {
        "OOF_SEALED",
        "CARDS_SEALED",
        "TARGET_LOCKED",
        "OUTCOMES_JOINED",
        "SAMPLE_500_SEALED",
        "VERIFIED",
    }

    runtime = ops.inspect_runtime(request, plan)
    if not isinstance(runtime, _RuntimeState):
        raise TypeError("runtime inspection must return a _RuntimeState")
    if runtime.phase == "SOURCES_READY":
        return replay_terminal()
    if runtime.phase in later_phases:
        raise TrustIntegrityError(
            f"execution phase {runtime.phase} is later than acquisition"
        )
    replay_after_unlock = False
    with ops.runner_lock(request):
        current = ops.inspect_runtime(request, plan)
        if not isinstance(current, _RuntimeState):
            raise TypeError("runtime inspection must return a _RuntimeState")
        if current.phase == "SOURCES_READY":
            replay_after_unlock = True
        elif current.phase in later_phases:
            raise TrustIntegrityError(
                f"execution phase {current.phase} is later than acquisition"
            )
        else:
            if current.has_progress and not request.resume:
                raise OperationalBlock(
                    "existing acquisition progress requires --resume"
                )
            if current.phase not in {None, "DESIGN_ACCEPTED", "ACQUIRING"}:
                raise TrustIntegrityError("execution phase authority differs")
            ops.begin_runtime(request, plan, current)
            summary = ops.run_stages(request, plan)
            if not isinstance(summary, AcquisitionSummary):
                raise TypeError("stage runner must return an AcquisitionSummary")
            return summary
    if replay_after_unlock:
        return replay_terminal()
    raise AssertionError("acquisition phase routing is incomplete")


def _absolute_supplied(path: Path, root: Path) -> Path:
    value = Path(path)
    return Path(
        os.path.abspath(os.fspath(value if value.is_absolute() else root / value))
    )


def _require_exact_path(
    root: Path,
    supplied: Path,
    relative: Path,
    label: str,
) -> Path:
    expected = root / relative
    candidate = _absolute_supplied(supplied, root)
    if candidate != expected:
        raise TrustIntegrityError(f"{label} path differs from the frozen contract")
    try:
        linked = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TrustIntegrityError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(linked.st_mode) and label != "legacy root":
        raise TrustIntegrityError(f"{label} must be a regular file")
    if label == "legacy root" and not stat.S_ISDIR(linked.st_mode):
        raise TrustIntegrityError("legacy root must be a directory")
    if stat.S_ISLNK(linked.st_mode) or resolved != expected:
        raise TrustIntegrityError(f"{label} path identity differs")
    return candidate


def _read_canonical_object(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    try:
        contents = path.read_bytes()
        value = json.loads(contents)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustIntegrityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != contents:
        raise TrustIntegrityError(f"{label} is not canonical JSON")
    return value, contents


def _validate_request_contract(
    request: _AcquisitionRequest,
) -> tuple[Path, dict[str, object], bytes]:
    from .v52_legacy import trusted_legacy_repository_root

    try:
        root = trusted_legacy_repository_root(request.root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustIntegrityError("repository root trust failed") from exc
    if not isinstance(request.run_spec, (RunSpec, SourceRunSpec)):
        raise TrustIntegrityError("run_spec must be an authenticated source authority")
    if request.years != _EXPECTED_YEARS:
        raise TrustIntegrityError("years must be the exact 2018:2024 range")
    if request.mode != "hybrid":
        raise TrustIntegrityError("mode must be hybrid")
    if type(request.resume) is not bool or type(request.plan_only) is not bool:
        raise TrustIntegrityError("resume and plan_only must be booleans")
    if request.plan_only and request.resume:
        raise TrustIntegrityError("--plan-only and --resume are mutually exclusive")
    if request.stop_after not in {None, "reference-inputs"}:
        raise TrustIntegrityError("stop_after differs from the closed runner contract")
    if (
        type(request.reserve_bytes) is not int
        or request.reserve_bytes < _MINIMUM_RESERVE_BYTES
    ):
        raise TrustIntegrityError("free-space reserve is below 20 GiB")

    supplied_contract = (
        root / _ACQUISITION_CONTRACT_RELATIVE
        if request.acquisition_contract_path is None
        else request.acquisition_contract_path
    )
    if isinstance(request.run_spec, SourceRunSpec):
        from .v52_query_plan import _load_static_contracts

        try:
            context = _load_static_contracts(
                root
                / "protocols/v5_2_qd/"
                "historical_walkforward_500_superseding_565534.json",
                supplied_contract,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TrustIntegrityError("source authority authentication failed") from exc
        if context.repository_root != root or context.authority != request.run_spec:
            raise TrustIntegrityError("source authority object differs")
        _require_exact_path(
            root, request.legacy_root, _LEGACY_ROOT_RELATIVE, "legacy root"
        )
        _require_exact_path(
            root,
            request.protected_inventory_path,
            _PROTECTED_INVENTORY_RELATIVE,
            "protected inventory",
        )
        raise TrustIntegrityError(
            "superseding source checkpoints are not enabled before Task 6"
        )
    run_path = _require_exact_path(
        root, root / _RUN_SPEC_RELATIVE, _RUN_SPEC_RELATIVE, "run spec"
    )
    contract_path = _require_exact_path(
        root, supplied_contract, _ACQUISITION_CONTRACT_RELATIVE, "acquisition contract"
    )
    _require_exact_path(root, request.legacy_root, _LEGACY_ROOT_RELATIVE, "legacy root")
    _require_exact_path(
        root,
        request.protected_inventory_path,
        _PROTECTED_INVENTORY_RELATIVE,
        "protected inventory",
    )
    try:
        canonical_run = load_run_spec(run_path)
    except (OSError, ValueError) as exc:
        raise TrustIntegrityError("run spec trust failed") from exc
    if request.run_spec != canonical_run:
        raise TrustIntegrityError("run spec object differs from the canonical path")
    contract, contract_contents = _read_canonical_object(
        contract_path, "acquisition contract"
    )
    if contract.get("run_id") != request.run_spec.run_id:
        raise TrustIntegrityError("acquisition contract run binding differs")
    if contract.get("required_years") != list(_EXPECTED_YEARS):
        raise TrustIntegrityError("acquisition contract years differ")
    if contract.get("minimum_free_space_reserve_bytes") != _MINIMUM_RESERVE_BYTES:
        raise TrustIntegrityError("acquisition contract reserve differs")
    legacy = contract.get("legacy_protection")
    if not isinstance(legacy, Mapping):
        raise TrustIntegrityError("legacy protection binding is missing")
    if (
        legacy.get("legacy_root") != _LEGACY_ROOT_RELATIVE.as_posix()
        or legacy.get("protected_inventory_path")
        != _PROTECTED_INVENTORY_RELATIVE.as_posix()
    ):
        raise TrustIntegrityError("legacy protection paths differ")
    return root, contract, contract_contents


def _real_authenticate(request: _AcquisitionRequest) -> _AcquisitionPlan:
    from .v52_legacy import PromotionDisposition, load_protected_legacy_catalog
    from .v52_legacy_decision_inventory import preview_legacy_decisions
    from .v52_query_plan import (
        build_static_batch,
        build_statcast_root_batch,
    )

    try:
        root, contract, contract_contents = _validate_request_contract(request)
        legacy = contract["legacy_protection"]
        assert isinstance(legacy, Mapping)
        protected_sha256 = legacy.get("protected_inventory_sha256")
        if not isinstance(protected_sha256, str):
            raise TrustIntegrityError("protected inventory hash is missing")
        catalog = load_protected_legacy_catalog(
            root,
            legacy_root=root / _LEGACY_ROOT_RELATIVE,
            protected_inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
            protected_inventory_sha256=protected_sha256,
        )
        static = build_static_batch(
            root / _RUN_SPEC_RELATIVE,
            root / _ACQUISITION_CONTRACT_RELATIVE,
        )
        statcast = build_statcast_root_batch(root)
        preview = preview_legacy_decisions(
            statcast,
            catalog,
            protected_inventory_sha256=protected_sha256,
        )
        dispositions = tuple(
            str(decision["disposition"]) for decision in preview.decisions
        )
        if PromotionDisposition.FATAL_INTEGRITY.value in dispositions:
            raise TrustIntegrityError("legacy plan contains fatal integrity evidence")
        return _AcquisitionPlan(
            contract_sha256=hashlib.sha256(contract_contents).hexdigest(),
            query_batch_ids=(
                static.batch_id,
                statcast.batch_id,
                preview.partition_batch.batch_id,
            ),
            legacy_dispositions=dispositions,
        )
    except TrustIntegrityError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrustIntegrityError(
            "acquisition authority authentication failed"
        ) from exc


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_lock_directory(
    descriptor: int,
    path: str | Path,
    *,
    dir_fd: int | None = None,
) -> None:
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise TrustIntegrityError("runner lock directory is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or not _same_identity(opened, linked)
    ):
        raise TrustIntegrityError("runner lock directory identity differs")


@contextmanager
def _safe_lock_parent(root: Path) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    descriptors: list[int] = []
    identities: list[tuple[int, str | Path, int | None]] = []
    try:
        try:
            root_descriptor = os.open(root, flags)
        except OSError as exc:
            raise TrustIntegrityError("runner lock directory is unavailable") from exc
        descriptors.append(root_descriptor)
        identities.append((root_descriptor, root, None))
        _validate_lock_directory(root_descriptor, root)
        _acquire_runner_flock(root_descriptor)
        current_descriptor = root_descriptor
        for component in _RUNNER_LOCK_RELATIVE.parent.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise TrustIntegrityError(
                    "runner lock directory is unavailable"
                ) from exc
            try:
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=current_descriptor,
                )
            except OSError as exc:
                raise TrustIntegrityError(
                    "runner lock directory is unavailable"
                ) from exc
            descriptors.append(child_descriptor)
            identities.append(
                (child_descriptor, component, current_descriptor)
            )
            _validate_lock_directory(
                child_descriptor,
                component,
                dir_fd=current_descriptor,
            )
            current_descriptor = child_descriptor
        try:
            yield current_descriptor
        finally:
            for descriptor, path, parent_descriptor in identities:
                _validate_lock_directory(
                    descriptor,
                    path,
                    dir_fd=parent_descriptor,
                )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _acquire_runner_flock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise OperationalBlock("another acquisition runner is active") from exc
        raise TrustIntegrityError("runner lock acquisition failed") from exc


def _validate_runner_lock(descriptor: int, parent_descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(
            _RUNNER_LOCK_RELATIVE.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TrustIntegrityError("runner lock identity is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or opened.st_nlink != 1
        or linked.st_nlink != 1
        or not _same_identity(opened, linked)
    ):
        raise TrustIntegrityError("runner lock identity differs")


@contextmanager
def _real_runner_lock(request: _AcquisitionRequest) -> Iterator[None]:
    root = Path(os.path.abspath(os.fspath(request.root)))
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    with _safe_lock_parent(root) as parent_descriptor:
        _acquire_runner_flock(parent_descriptor)
        try:
            descriptor = os.open(
                _RUNNER_LOCK_RELATIVE.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise TrustIntegrityError("runner lock is unavailable") from exc
        try:
            _validate_runner_lock(descriptor, parent_descriptor)
            _acquire_runner_flock(descriptor)
            _validate_runner_lock(descriptor, parent_descriptor)
            token = _ACTIVE_RUNNER.set(
                _RunnerGuard(descriptor, parent_descriptor, root)
            )
            try:
                yield
            finally:
                try:
                    _validate_runner_lock(descriptor, parent_descriptor)
                finally:
                    _ACTIVE_RUNNER.reset(token)
        finally:
            os.close(descriptor)


def _validate_active_runner(
    request: _AcquisitionRequest,
    runner_guard: _RunnerGuard | None = None,
) -> None:
    binding = _ACTIVE_RUNNER.get() if runner_guard is None else runner_guard
    root = Path(os.path.abspath(os.fspath(request.root)))
    if binding is None or binding.root != root:
        raise TrustIntegrityError("active runner identity is unavailable")
    _validate_runner_lock(binding.descriptor, binding.parent_descriptor)


def _current_runner_guard(request: _AcquisitionRequest) -> _RunnerGuard:
    guard = _ACTIVE_RUNNER.get()
    _validate_active_runner(request, guard)
    assert guard is not None
    return guard


def _real_before_network(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
    *,
    stage: str,
    query_batch_ids: tuple[str, ...],
    query: LogicalQuery,
    runner_guard: _RunnerGuard | None = None,
    disk_budget: _DiskBudget | None = None,
) -> None:
    from .execution_registry import (
        load_execution_registry,
        load_latest_acquisition_checkpoint,
    )
    from .v52_query_plan import load_query_batch

    if _real_authenticate(request) != plan:
        raise TrustIntegrityError("acquisition authority changed before fetch")
    root = Path(os.path.abspath(os.fspath(request.root)))
    try:
        registry = load_execution_registry(root, request.run_spec)
        checkpoint = load_latest_acquisition_checkpoint(root, request.run_spec)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustIntegrityError("fetch checkpoint authority is unavailable") from exc
    query_id = logical_query_id(query)
    if (
        registry.phase != "ACQUIRING"
        or checkpoint is None
        or checkpoint.stage != stage
        or checkpoint.query_batch_ids != query_batch_ids
        or query_id not in checkpoint.pending_query_ids
    ):
        raise TrustIntegrityError("query is not pending under current checkpoint authority")
    occurrences = 0
    try:
        for batch_id in query_batch_ids:
            batch = load_query_batch(root, batch_id)
            occurrences += sum(item == query for item in batch.queries)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrustIntegrityError("query batch authority changed before fetch") from exc
    if occurrences != 1:
        raise TrustIntegrityError("pending query batch membership differs")
    _validate_active_runner(request, runner_guard)
    if disk_budget is not None:
        disk_budget.recheck()
        return
    required = request.reserve_bytes + fetch_limits(query).max_bytes
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise OperationalBlock("free-space reserve is unavailable") from exc
    if free < required:
        raise OperationalBlock("free-space reserve would be violated by fetch")


@dataclass(frozen=True)
class _StageResult:
    checkpoint: object
    fetched: int
    resumed: int
    network_attempts: int


@dataclass(frozen=True)
class _RemainingWaveResult:
    fetched: int
    resumed: int
    network_attempts: int
    splits: tuple[tuple[str, SplitRequired], ...]


class _StageStopped(BaseException):
    """Internal cooperative stop signal; never exposed as a stage outcome."""


@dataclass(frozen=True)
class _StageFailure:
    query_id: str
    submission_index: int
    error: BaseException


def _stage_failure_rank(error: BaseException) -> int:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return 0
    if not isinstance(
        error,
        (
            TrustIntegrityError,
            SplitRequired,
            OperationalBlock,
            TerminalAcquisitionFailure,
        ),
    ):
        return 1
    if isinstance(error, TrustIntegrityError):
        return 2
    if isinstance(error, SplitRequired):
        return 3
    if isinstance(error, OperationalBlock):
        return 4
    return 5


def _is_fatal_stage_error(error: BaseException) -> bool:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return True
    return not isinstance(
        error,
        (
            _StageStopped,
            TrustIntegrityError,
            SplitRequired,
            OperationalBlock,
            TerminalAcquisitionFailure,
        ),
    )


def _select_stage_failure(failures: Sequence[_StageFailure]) -> _StageFailure | None:
    if not failures:
        return None
    return min(
        failures,
        key=lambda failure: (
            _stage_failure_rank(failure.error),
            failure.query_id,
            failure.submission_index,
        ),
    )


def _stage_populations(
    store: V52EvidenceStore,
    batches: Sequence[object],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    completed: list[str] = []
    pending: list[str] = []
    quarantined: list[str] = []
    seen: set[str] = set()
    for batch in batches:
        queries = getattr(batch, "queries", None)
        if not isinstance(queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in queries
        ):
            raise TypeError("stage batch queries must be a LogicalQuery tuple")
        for query in queries:
            query_id = logical_query_id(query)
            if query_id in seen:
                raise TrustIntegrityError("stage query occurs in multiple batches")
            seen.add(query_id)
            progress = _integrity_boundary(
                "query evidence replay",
                lambda query=query: store.read_verified_query_progress(query),
            )
            if progress.status == "SUCCESS":
                completed.append(query_id)
            elif progress.status == "QUARANTINED":
                quarantined.append(query_id)
            else:
                pending.append(query_id)
    return tuple(sorted(completed)), tuple(sorted(pending)), tuple(sorted(quarantined))


def _remaining_stage_evidence(
    root: Path,
    participant_evidence: Sequence[Path],
    statcast_root_batch_id: str,
) -> tuple[Path, ...]:
    if (
        not isinstance(statcast_root_batch_id, str)
        or len(statcast_root_batch_id) != 64
        or any(character not in "0123456789abcdef" for character in statcast_root_batch_id)
    ):
        raise TrustIntegrityError("Statcast root batch ID differs")
    root = Path(os.path.abspath(os.fspath(root)))
    participant = tuple(Path(path) for path in participant_evidence)
    if len(participant) != 7 or len(participant) != len(set(participant)):
        raise TrustIntegrityError("participant stage evidence authority differs")
    result = tuple(
        sorted(
            (
                *participant,
                root / _PROTECTED_INVENTORY_RELATIVE,
                root
                / "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json",
                root
                / "audit/v5_2_qd/historical/acquisition/query_batches"
                / f"{statcast_root_batch_id}.json",
            ),
            key=os.fspath,
        )
    )
    if len(result) != 10 or len(result) != len(set(result)):
        raise TrustIntegrityError("REMAINING stage evidence authority differs")
    return result


def _remaining_populations(
    store: V52EvidenceStore,
    batches: Sequence[object],
    split_query_ids: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    split = tuple(sorted(split_query_ids))
    if len(split) != len(set(split)):
        raise TrustIntegrityError("REMAINING split population contains duplicates")
    split_set = set(split)
    completed: list[str] = []
    pending: list[str] = []
    quarantined: list[str] = []
    seen: set[str] = set()
    for batch in batches:
        queries = getattr(batch, "queries", None)
        if not isinstance(queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in queries
        ):
            raise TypeError("REMAINING batches must contain LogicalQuery tuples")
        for query in queries:
            query_id = logical_query_id(query)
            if query_id in seen:
                raise TrustIntegrityError("REMAINING query occurs in multiple batches")
            seen.add(query_id)
            if query_id in split_set:
                continue
            progress = _integrity_boundary(
                "REMAINING query evidence replay",
                lambda query=query: store.read_verified_query_progress(query),
            )
            if progress.status == "SUCCESS":
                completed.append(query_id)
            elif progress.status == "QUARANTINED":
                quarantined.append(query_id)
            else:
                pending.append(query_id)
    if not split_set <= seen:
        raise TrustIntegrityError("REMAINING split parent is outside its batches")
    return (
        tuple(sorted(completed)),
        tuple(sorted(pending)),
        tuple(sorted(quarantined)),
        split,
    )


def _closed_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _closed_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_closed_value(child) for child in value]
    return value


def _validate_remaining_decisions(
    partition_batch: object,
    inventory: object,
    catalog: object,
) -> tuple[tuple[LogicalQuery, object], ...]:
    from .v52_legacy import (
        PromotionDisposition,
        ProtectedLegacyCatalog,
        audit_legacy_candidates_evidence,
    )

    queries = getattr(partition_batch, "queries", None)
    payload = getattr(inventory, "payload", None)
    if (
        not isinstance(queries, tuple)
        or len(queries) != 48
        or any(not isinstance(query, LogicalQuery) for query in queries)
        or not isinstance(payload, Mapping)
        or not isinstance(catalog, ProtectedLegacyCatalog)
    ):
        raise TrustIntegrityError("REMAINING decision authority differs")
    decisions = payload.get("decisions")
    counts = payload.get("counts")
    if (
        not isinstance(decisions, tuple)
        or len(decisions) != 48
        or not all(isinstance(decision, Mapping) for decision in decisions)
        or not isinstance(counts, Mapping)
        or counts.get("promoted_count") != 1
        or counts.get("fetch_required_count") != 47
        or counts.get("fatal_integrity_count") != 0
    ):
        raise TrustIntegrityError("REMAINING decision counts differ")
    sealed = {decision.get("candidate_query_id"): decision for decision in decisions}
    query_ids = tuple(logical_query_id(query) for query in queries)
    if len(sealed) != 48 or set(sealed) != set(query_ids):
        raise TrustIntegrityError("REMAINING decision bijection differs")

    try:
        fresh = audit_legacy_candidates_evidence(queries, catalog)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrustIntegrityError("REMAINING fresh decision audit failed") from exc
    promoted: list[tuple[LogicalQuery, object]] = []
    dispositions = {"PROMOTED": 0, "FETCH_REQUIRED": 0}
    for query, evidence in zip(queries, fresh, strict=True):
        query_id = logical_query_id(query)
        record = sealed[query_id]
        disposition = evidence.decision.disposition.value
        expected_acquisition = (
            "PROTECTED_LEGACY_REFERENCE"
            if disposition == "PROMOTED"
            else "FETCH_REQUIRED"
        )
        requested = query.requested_range
        expected_range = {
            "year": requested.get("year"),
            "start_date": requested.get("start_date"),
            "end_date": requested.get("end_date"),
        }
        if (
            disposition not in dispositions
            or evidence.decision.query_id != query_id
            or record.get("parent_query_id") != query.parent_query_id
            or _closed_value(record.get("range")) != expected_range
            or record.get("disposition") != disposition
            or _closed_value(record.get("reason_codes"))
            != [reason.value for reason in evidence.decision.reason_codes]
            or _closed_value(record.get("candidate_lineage"))
            != _closed_value(evidence.candidate_lineage)
            or record.get("semantic_sha256") != evidence.semantic_sha256
            or requested.get("acquisition") != expected_acquisition
        ):
            raise TrustIntegrityError("REMAINING sealed decision replay differs")
        dispositions[disposition] += 1
        if evidence.decision.disposition is PromotionDisposition.PROMOTED:
            promoted.append((query, evidence.decision))
    if dispositions != {"PROMOTED": 1, "FETCH_REQUIRED": 47} or len(promoted) != 1:
        raise TrustIntegrityError("REMAINING fresh decision counts differ")
    return tuple(promoted)


def _promote_remaining(
    store: V52EvidenceStore,
    catalog: object,
    promoted: Sequence[tuple[LogicalQuery, object]],
) -> tuple[int, int]:
    from .v52_legacy import (
        PromotionDecision,
        ProtectedLegacyCatalog,
        promote_legacy_candidate,
    )

    if not isinstance(store, V52EvidenceStore) or not isinstance(
        catalog, ProtectedLegacyCatalog
    ):
        raise TypeError("REMAINING promotion authority differs")
    values = tuple(promoted)
    if len(values) != 1:
        raise TrustIntegrityError("REMAINING requires exactly one promotion")
    newly_promoted = 0
    resumed = 0
    for query, decision in values:
        if not isinstance(query, LogicalQuery) or not isinstance(
            decision, PromotionDecision
        ):
            raise TrustIntegrityError("REMAINING promotion decision differs")
        progress = _integrity_boundary(
            "REMAINING promotion replay",
            lambda: store.read_verified_query_progress(query),
        )
        if progress.status == "QUARANTINED":
            raise OperationalBlock("REMAINING promotion is quarantined")
        if progress.status == "SUCCESS":
            published = progress.published_evidence
            if (
                not isinstance(published, PublishedEvidence)
                or published.storage_mode != "PROTECTED_LEGACY_REFERENCE"
            ):
                raise TrustIntegrityError("REMAINING promotion storage mode differs")
            resumed += 1
            continue
        try:
            promoted_evidence = promote_legacy_candidate(
                store,
                query,
                catalog,
                decision,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TrustIntegrityError("REMAINING promotion integrity failed") from exc
        if promoted_evidence.storage_mode != "PROTECTED_LEGACY_REFERENCE":
            raise TrustIntegrityError("REMAINING promotion storage mode differs")
        newly_promoted += 1
    return newly_promoted, resumed


def _validate_terminal_remaining_promotion(
    store: V52EvidenceStore,
    promoted: Sequence[tuple[LogicalQuery, object]],
) -> PublishedEvidence:
    values = tuple(promoted)
    if not isinstance(store, V52EvidenceStore) or len(values) != 1:
        raise TrustIntegrityError("terminal REMAINING promotion authority differs")
    query, _decision = values[0]
    if not isinstance(query, LogicalQuery):
        raise TrustIntegrityError("terminal REMAINING promotion query differs")
    progress = _integrity_boundary(
        "terminal REMAINING promotion replay",
        lambda: store.read_verified_query_progress(query),
    )
    published = getattr(progress, "published_evidence", None)
    if (
        getattr(progress, "status", None) != "SUCCESS"
        or not isinstance(published, PublishedEvidence)
        or published.query_id != logical_query_id(query)
        or published.storage_mode != "PROTECTED_LEGACY_REFERENCE"
    ):
        raise TrustIntegrityError(
            "terminal REMAINING promotion storage authority differs"
        )
    return published


def _run_remaining_wave(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
    store: V52EvidenceStore,
    *,
    batches: Sequence[object],
    active_queries: Sequence[LogicalQuery],
    fetcher: Callable[..., AttemptCapture],
    query_acquirer: Callable[..., PublishedEvidence] = acquire_with_retries,
    parser: Callable[[bytes, LogicalQuery], Mapping[str, object]] | None = None,
    network_gate: Callable[..., None] = _real_before_network,
    runner_guard: _RunnerGuard | None = None,
    disk_budget: _DiskBudget | None = None,
) -> _RemainingWaveResult:
    """Run one REMAINING leaf wave and join it before exposing split results."""
    from .v52_source_contracts import recompute_query_sidecar

    batch_ids = tuple(getattr(batch, "batch_id", None) for batch in batches)
    if not batch_ids or any(not isinstance(value, str) for value in batch_ids):
        raise TypeError("remaining batches must expose batch IDs")
    selected_parser = (
        (lambda raw, query: recompute_query_sidecar(query, raw))
        if parser is None
        else parser
    )
    selected_queries = tuple(active_queries)
    if not selected_queries or any(
        not isinstance(query, LogicalQuery) for query in selected_queries
    ):
        raise TypeError("remaining active queries must be a nonempty LogicalQuery set")
    known = {
        logical_query_id(query)
        for batch in batches
        for query in getattr(batch, "queries", ())
    }
    selected_ids = tuple(logical_query_id(query) for query in selected_queries)
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= known:
        raise TrustIntegrityError("remaining active-leaf authority differs")
    routes = {logical_query_id(query): _query_host(query) for query in selected_queries}
    if any(host is None for host in routes.values()):
        raise TrustIntegrityError("remaining active leaves must require network evidence")

    resumed = 0
    by_host: dict[str, list[LogicalQuery]] = {host: [] for host in _HOST_WORKERS}
    for query in selected_queries:
        progress = _integrity_boundary(
            "remaining query evidence replay",
            lambda query=query: store.read_verified_query_progress(query),
        )
        if progress.status == "SUCCESS":
            resumed += 1
        elif progress.status == "QUARANTINED":
            raise OperationalBlock("REMAINING contains quarantined query evidence")
        else:
            host = routes[logical_query_id(query)]
            assert isinstance(host, str)
            by_host[host].append(query)

    for query in sorted(
        (query for queries in by_host.values() for query in queries),
        key=logical_query_id,
    ):
        _integrity_boundary(
            "remaining query registration",
            lambda query=query: store.register_query(query),
        )

    network_attempts = 0
    attempts_by_query: dict[str, int] = {}
    counter_lock = threading.Lock()
    stop = threading.Event()

    def check_stop() -> None:
        if stop.is_set():
            raise _StageStopped()

    def stop_aware_sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise _StageStopped()

    def counted_fetcher(
        query: LogicalQuery,
        *,
        timeout: float,
        max_bytes: int,
    ) -> AttemptCapture:
        nonlocal network_attempts
        check_stop()
        query_id = logical_query_id(query)
        with counter_lock:
            check_stop()
            network_attempts += 1
            attempts_by_query[query_id] = attempts_by_query.get(query_id, 0) + 1
        try:
            check_stop()
        except _StageStopped:
            with counter_lock:
                network_attempts -= 1
                attempts_by_query[query_id] -= 1
            raise
        return fetcher(query, timeout=timeout, max_bytes=max_bytes)

    def guarded_gate(item: LogicalQuery) -> None:
        check_stop()
        network_gate(
            request,
            plan,
            stage="REMAINING",
            query_batch_ids=batch_ids,
            query=item,
            runner_guard=runner_guard,
            disk_budget=disk_budget,
        )

    def acquire_reserved(query: LogicalQuery) -> bool:
        query_acquirer(
            store,
            query,
            selected_parser,
            policy=RetryPolicy(),
            limits=fetch_limits(query),
            before_network=guarded_gate,
            fetcher=counted_fetcher,
            sleeper=stop_aware_sleep,
        )
        progress = _integrity_boundary(
            "remaining query evidence replay",
            lambda: store.read_verified_query_progress(query),
        )
        if progress.status != "SUCCESS":
            raise RuntimeError("remaining query returned without verified success")
        with counter_lock:
            return attempts_by_query.get(logical_query_id(query), 0) > 0

    def run_one(query: LogicalQuery) -> bool:
        try:
            check_stop()
            if disk_budget is None:
                return acquire_reserved(query)
            with disk_budget.reserve(query):
                check_stop()
                return acquire_reserved(query)
        except BaseException as error:
            if _is_fatal_stage_error(error):
                stop.set()
            raise

    futures: list[concurrent.futures.Future[bool]] = []
    submissions: dict[concurrent.futures.Future[bool], tuple[str, int]] = {}
    executors: list[concurrent.futures.ThreadPoolExecutor] = []
    coordinator_error: BaseException | None = None
    try:
        submission_index = 0
        for host, queries in by_host.items():
            if not queries:
                continue
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=_HOST_WORKERS[host],
                thread_name_prefix=f"v52-{host}",
            )
            executors.append(executor)
            for query in queries:
                future = executor.submit(run_one, query)
                futures.append(future)
                submissions[future] = (logical_query_id(query), submission_index)
                submission_index += 1
        for future in concurrent.futures.as_completed(futures):
            if future.cancelled():
                continue
            error = future.exception()
            if stop.is_set() or error is not None and _is_fatal_stage_error(error):
                stop.set()
                for pending in futures:
                    pending.cancel()
                break
    except BaseException as error:
        coordinator_error = error
        stop.set()
        for future in futures:
            future.cancel()
    finally:
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=True)
    if coordinator_error is not None:
        raise coordinator_error

    fetched = 0
    failures: list[_StageFailure] = []
    splits: list[tuple[str, SplitRequired]] = []
    for future, (query_id, index) in submissions.items():
        if future.cancelled():
            continue
        error = future.exception()
        if error is None:
            fetched += bool(future.result())
        elif isinstance(error, SplitRequired):
            splits.append((query_id, error))
        elif not isinstance(error, _StageStopped):
            failures.append(_StageFailure(query_id, index, error))
    selected_failure = _select_stage_failure(failures)
    if selected_failure is not None:
        raise selected_failure.error
    return _RemainingWaveResult(
        fetched=fetched,
        resumed=resumed,
        network_attempts=network_attempts,
        splits=tuple(sorted(splits, key=lambda item: item[0])),
    )


def _query_host(query: LogicalQuery) -> str | None:
    if query.endpoint.startswith("disposition://"):
        return None
    parsed = urlparse(query.endpoint)
    if parsed.scheme != "https" or parsed.hostname not in _HOST_WORKERS:
        raise TrustIntegrityError("query host is outside the frozen acquisition pools")
    return parsed.hostname


def _execute_stage(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
    store: V52EvidenceStore,
    *,
    stage: str,
    batches: Sequence[object],
    active_batches: Sequence[object] | None = None,
    stage_evidence: Sequence[Path],
    expected_prior: str | None,
    fetcher: Callable[..., AttemptCapture],
    checkpoint_publisher: Callable[..., object] | None = None,
    query_acquirer: Callable[..., PublishedEvidence] = acquire_with_retries,
    parser: Callable[[bytes, LogicalQuery], Mapping[str, object]] | None = None,
    network_gate: Callable[..., None] = _real_before_network,
    runner_guard: _RunnerGuard | None = None,
    disk_budget: _DiskBudget | None = None,
) -> _StageResult:
    from .execution_registry import publish_acquisition_checkpoint
    from .v52_source_contracts import recompute_query_sidecar

    if not isinstance(store, V52EvidenceStore):
        raise TypeError("store must be a V52EvidenceStore")
    batch_ids = tuple(getattr(batch, "batch_id", None) for batch in batches)
    if not batch_ids or any(not isinstance(value, str) for value in batch_ids):
        raise TypeError("stage batches must expose batch IDs")
    selected_parser = (
        (lambda raw, query: recompute_query_sidecar(query, raw))
        if parser is None
        else parser
    )

    def publish(prior: str | None) -> object:
        completed, pending, quarantined = _stage_populations(store, batches)
        values = {
            "expected_prior_checkpoint_sha256": prior,
            "stage": stage,
            "query_batch_ids": batch_ids,
            "completed_query_ids": completed,
            "pending_query_ids": pending,
            "quarantined_query_ids": quarantined,
            "split_query_ids": (),
            "stage_evidence_receipts": tuple(stage_evidence),
            "split_receipt_paths": (),
        }
        if checkpoint_publisher is not None:
            return checkpoint_publisher(**values)
        return _integrity_boundary(
            "acquisition checkpoint",
            lambda: publish_acquisition_checkpoint(
                request.root,
                request.run_spec,
                **values,
            ),
        )

    selected_active_batches = batches if active_batches is None else active_batches
    if any(batch not in batches for batch in selected_active_batches):
        raise TrustIntegrityError("active stage batches differ from checkpoint batches")
    active_queries = tuple(
        query for batch in selected_active_batches for query in batch.queries
    )
    routes = {logical_query_id(query): _query_host(query) for query in active_queries}

    checkpoint = publish(expected_prior)
    prior = getattr(checkpoint, "receipt_sha256", None)
    if not isinstance(prior, str):
        raise TypeError("checkpoint publisher must return a receipt SHA-256")
    if getattr(checkpoint, "quarantined_query_ids", ()):
        raise OperationalBlock(f"{stage} contains quarantined query evidence")

    resumed = 0
    derived: list[LogicalQuery] = []
    by_host: dict[str, list[LogicalQuery]] = {host: [] for host in _HOST_WORKERS}
    for query in active_queries:
        progress = _integrity_boundary(
            "query evidence replay",
            lambda query=query: store.read_verified_query_progress(query),
        )
        if progress.status == "SUCCESS":
            resumed += 1
        elif progress.status == "QUARANTINED":
            raise OperationalBlock(f"{stage} contains quarantined query evidence")
        elif routes[logical_query_id(query)] is None:
            derived.append(query)
        else:
            host = routes[logical_query_id(query)]
            assert isinstance(host, str)
            by_host[host].append(query)

    network_attempts = 0
    attempts_by_query: dict[str, int] = {}
    counter_lock = threading.Lock()
    stop = threading.Event()

    def check_stop() -> None:
        if stop.is_set():
            raise _StageStopped()

    def stop_aware_sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise _StageStopped()

    def counted_fetcher(
        query: LogicalQuery,
        *,
        timeout: float,
        max_bytes: int,
    ) -> AttemptCapture:
        nonlocal network_attempts
        check_stop()
        query_id = logical_query_id(query)
        with counter_lock:
            check_stop()
            network_attempts += 1
            attempts_by_query[query_id] = attempts_by_query.get(query_id, 0) + 1
        try:
            check_stop()
        except _StageStopped:
            with counter_lock:
                network_attempts -= 1
                attempts_by_query[query_id] -= 1
            raise
        return fetcher(query, timeout=timeout, max_bytes=max_bytes)

    for query in derived:
        semantics = dict(selected_parser(b"", query))
        semantics["reason"] = query.requested_range.get("reason")

        def publish_derived() -> PublishedEvidence:
            with store.acquire_query(query):
                return store.publish_derived_disposition(query, semantics)

        _integrity_boundary("derived disposition", publish_derived)

    for query in sorted(
        (query for queries in by_host.values() for query in queries),
        key=logical_query_id,
    ):
        _integrity_boundary(
            "stage query registration",
            lambda query=query: store.register_query(query),
        )

    def guarded_gate(item: LogicalQuery) -> None:
        check_stop()
        network_gate(
            request,
            plan,
            stage=stage,
            query_batch_ids=batch_ids,
            query=item,
            runner_guard=runner_guard,
            disk_budget=disk_budget,
        )

    def acquire_reserved(query: LogicalQuery) -> bool:
        query_acquirer(
            store,
            query,
            selected_parser,
            policy=RetryPolicy(),
            limits=fetch_limits(query),
            before_network=guarded_gate,
            fetcher=counted_fetcher,
            sleeper=stop_aware_sleep,
        )
        progress = _integrity_boundary(
            "query evidence replay",
            lambda: store.read_verified_query_progress(query),
        )
        if progress.status != "SUCCESS":
            raise RuntimeError("query acquirer returned without verified success")
        with counter_lock:
            return attempts_by_query.get(logical_query_id(query), 0) > 0

    def acquire_one(query: LogicalQuery) -> bool:
        check_stop()
        if disk_budget is None:
            return acquire_reserved(query)
        with disk_budget.reserve(query):
            check_stop()
            return acquire_reserved(query)

    def run_one(query: LogicalQuery) -> bool:
        try:
            return acquire_one(query)
        except BaseException as error:
            if _is_fatal_stage_error(error):
                stop.set()
            raise

    futures: list[concurrent.futures.Future[bool]] = []
    submissions: dict[concurrent.futures.Future[bool], tuple[str, int]] = {}
    executors: list[concurrent.futures.ThreadPoolExecutor] = []
    coordinator_error: BaseException | None = None

    def cancel_all() -> None:
        for future in futures:
            future.cancel()

    try:
        submission_index = 0
        for host, queries in by_host.items():
            if not queries:
                continue
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=_HOST_WORKERS[host],
                thread_name_prefix=f"v52-{host}",
            )
            executors.append(executor)
            for query in queries:
                future = executor.submit(run_one, query)
                futures.append(future)
                submissions[future] = (logical_query_id(query), submission_index)
                submission_index += 1
        for future in concurrent.futures.as_completed(futures):
            if future.cancelled():
                continue
            error = future.exception()
            if stop.is_set() or (
                error is not None and _is_fatal_stage_error(error)
            ):
                stop.set()
                cancel_all()
                break
    except BaseException as error:
        coordinator_error = error
        stop.set()
        cancel_all()
    finally:
        if stop.is_set():
            cancel_all()
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=True)

    if coordinator_error is not None:
        raise coordinator_error

    results: list[bool] = []
    failures: list[_StageFailure] = []
    for future, (query_id, index) in submissions.items():
        if future.cancelled():
            continue
        error = future.exception()
        if error is None:
            results.append(future.result())
        elif not isinstance(error, _StageStopped):
            failures.append(_StageFailure(query_id, index, error))

    selected_failure = _select_stage_failure(failures)
    if selected_failure is not None and not isinstance(
        selected_failure.error,
        TerminalAcquisitionFailure,
    ):
        if isinstance(selected_failure.error, SplitRequired):
            raise TrustIntegrityError(
                f"{stage} cannot split a query"
            ) from selected_failure.error
        raise selected_failure.error
    if selected_failure is not None:
        terminal_checkpoint = publish(prior)
        if getattr(terminal_checkpoint, "quarantined_query_ids", ()):
            message = f"{stage} contains quarantined query evidence"
        elif getattr(terminal_checkpoint, "pending_query_ids", ()):
            message = f"{stage} contains pending query evidence"
        else:
            raise TrustIntegrityError(
                f"{stage} terminal failure has no bound query status"
            ) from selected_failure.error
        raise OperationalBlock(message) from selected_failure.error

    if not derived and not futures:
        return _StageResult(
            checkpoint=checkpoint,
            fetched=0,
            resumed=resumed,
            network_attempts=network_attempts,
        )
    checkpoint = publish(prior)
    completed, pending, quarantined = _stage_populations(store, batches)
    if pending or quarantined:
        raise OperationalBlock(f"{stage} is not terminal")
    return _StageResult(
        checkpoint=checkpoint,
        fetched=sum(results),
        resumed=resumed,
        network_attempts=network_attempts,
    )


def _ensure_canonical_raw_parent(request: _AcquisitionRequest) -> Path:
    namespaces = request.run_spec.raw.get("namespaces")
    raw_value = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    relative = Path(raw_value) if isinstance(raw_value, str) else Path()
    expected = Path("data/v5_2_qd/historical/raw")
    if relative != expected:
        raise TrustIntegrityError("raw evidence namespace differs")
    root = Path(os.path.abspath(os.fspath(request.root)))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        for component in relative.parent.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=current)
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or not _same_identity(opened, linked)
            ):
                os.close(child)
                raise TrustIntegrityError("raw evidence parent identity differs")
            descriptors.append(child)
            current = child
        return root / relative
    except TrustIntegrityError:
        raise
    except OSError as exc:
        raise TrustIntegrityError("raw evidence parent is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_schedule_inventory_schema(root: Path, inventory: object) -> None:
    import jsonschema

    expected_path = root / _SCHEDULE_INVENTORY_RELATIVE
    if getattr(inventory, "path", None) != expected_path:
        raise TrustIntegrityError("schedule inventory path differs")
    schema_path = _require_exact_path(
        root,
        root / _SCHEDULE_INVENTORY_SCHEMA_RELATIVE,
        _SCHEDULE_INVENTORY_SCHEMA_RELATIVE,
        "schedule inventory schema",
    )
    schema, _ = _read_canonical_object(schema_path, "schedule inventory schema")
    payload, contents = _read_canonical_object(expected_path, "schedule inventory")
    if hashlib.sha256(contents).hexdigest() != getattr(inventory, "sha256", None):
        raise TrustIntegrityError("schedule inventory hash differs")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(payload)
    except (jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        raise TrustIntegrityError("schedule inventory schema validation failed") from exc


def _revalidate_gate_directory(
    descriptor: int,
    path: str | Path,
    parent_descriptor: int | None,
) -> None:
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(
            path,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TrustIntegrityError("timezone gate ancestor is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or not _same_identity(opened, linked)
    ):
        raise TrustIntegrityError("timezone gate ancestor identity differs")


def _required_gate_leaf_exists(root: Path, relative: Path) -> bool:
    if relative.is_absolute() or not relative.parts:
        raise TrustIntegrityError("timezone gate path differs")
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    absolute_root = Path(os.path.abspath(os.fspath(root)))
    descriptors: list[int] = []
    directories: list[tuple[int, str | Path, int | None]] = []
    final_descriptor = -1
    try:
        try:
            current = os.open(os.sep, directory_flags)
        except OSError as exc:
            raise TrustIntegrityError("timezone gate root is unavailable") from exc
        descriptors.append(current)
        directories.append((current, Path(os.sep), None))
        _revalidate_gate_directory(current, Path(os.sep), None)
        for component in (*absolute_root.parts[1:], *relative.parent.parts):
            parent = current
            try:
                current = os.open(component, directory_flags, dir_fd=parent)
            except FileNotFoundError:
                for descriptor, path, ancestor in directories:
                    _revalidate_gate_directory(descriptor, path, ancestor)
                return False
            except OSError as exc:
                raise TrustIntegrityError(
                    "timezone gate ancestor is unavailable or unsafe"
                ) from exc
            descriptors.append(current)
            directories.append((current, component, parent))
            _revalidate_gate_directory(current, component, parent)
        try:
            final_descriptor = os.open(
                relative.name,
                file_flags,
                dir_fd=current,
            )
        except FileNotFoundError:
            for descriptor, path, parent in directories:
                _revalidate_gate_directory(descriptor, path, parent)
            return False
        except OSError as exc:
            raise TrustIntegrityError("timezone gate path is unsafe") from exc
        try:
            opened = os.fstat(final_descriptor)
            linked = os.stat(
                relative.name,
                dir_fd=current,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise TrustIntegrityError("timezone gate path is unavailable") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_nlink != 1
            or linked.st_nlink != 1
            or not _same_identity(opened, linked)
        ):
            raise TrustIntegrityError("timezone gate path identity differs")
        for descriptor, path, parent in directories:
            _revalidate_gate_directory(descriptor, path, parent)
        rebound = os.stat(
            relative.name,
            dir_fd=current,
            follow_symlinks=False,
        )
        if not _same_identity(opened, rebound) or rebound.st_nlink != 1:
            raise TrustIntegrityError("timezone gate path identity changed")
        return True
    finally:
        if final_descriptor >= 0:
            os.close(final_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _load_required_timezone_gate(root: Path) -> object:
    from .v52_timezone_review import load_verified_timezone_gate

    required_gate_paths = (
        Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json"),
        Path("audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json"),
        Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"),
        Path("config/v5_2_qd/venue_timezones.json"),
    )
    for relative in required_gate_paths:
        if not _required_gate_leaf_exists(root, relative):
            raise OperationalBlock(
                "timezone review approvals and seal are required"
            )
    return _integrity_boundary(
        "timezone gate",
        lambda: load_verified_timezone_gate(root),
    )


def _timezone_checkpoint_file_identity(
    metadata: os.stat_result,
) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_timezone_checkpoint_evidence_snapshot(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
) -> bytes:
    absolute_root = Path(os.path.abspath(os.fspath(root)))
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise ValueError("timezone checkpoint evidence path differs") from exc
    if not relative.parts or max_bytes < 0:
        raise ValueError("timezone checkpoint evidence path or bound differs")

    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors: list[int] = []
    directories: list[tuple[int, str | Path, int | None]] = []
    file_descriptor = -1

    def revalidate_directory(
        descriptor: int,
        component: str | Path,
        parent: int | None,
    ) -> None:
        try:
            opened = os.fstat(descriptor)
            linked = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError(
                "timezone checkpoint evidence ancestor is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or not _same_identity(opened, linked)
        ):
            raise ValueError(
                "timezone checkpoint evidence ancestor identity differs"
            )

    try:
        try:
            current = os.open(os.sep, directory_flags)
        except OSError as exc:
            raise ValueError(
                "timezone checkpoint evidence root is unavailable"
            ) from exc
        descriptors.append(current)
        directories.append((current, Path(os.sep), None))
        revalidate_directory(current, Path(os.sep), None)
        for component in (*absolute_root.parts[1:], *relative.parent.parts):
            parent = current
            try:
                current = os.open(component, directory_flags, dir_fd=parent)
            except OSError as exc:
                raise ValueError(
                    "timezone checkpoint evidence ancestor is unsafe"
                ) from exc
            descriptors.append(current)
            directories.append((current, component, parent))
            revalidate_directory(current, component, parent)
        try:
            file_descriptor = os.open(
                relative.name,
                file_flags,
                dir_fd=current,
            )
        except OSError as exc:
            raise ValueError(
                "timezone checkpoint evidence must be a no-follow regular file"
            ) from exc
        try:
            before = os.fstat(file_descriptor)
            linked = os.stat(
                relative.name,
                dir_fd=current,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError(
                "timezone checkpoint evidence identity is unavailable"
            ) from exc
        before_identity = _timezone_checkpoint_file_identity(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before_identity != _timezone_checkpoint_file_identity(linked)
        ):
            raise ValueError(
                "timezone checkpoint evidence must be an unaliased regular file"
            )
        if before.st_size > max_bytes:
            raise ValueError("timezone checkpoint evidence exceeds its read bound")

        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(file_descriptor, 64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    "timezone checkpoint evidence exceeds its read bound"
                )
            chunks.append(chunk)

        try:
            after = os.fstat(file_descriptor)
            rebound = os.stat(
                relative.name,
                dir_fd=current,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError(
                "timezone checkpoint evidence changed while reading"
            ) from exc
        after_identity = _timezone_checkpoint_file_identity(after)
        if (
            before_identity != after_identity
            or after_identity != _timezone_checkpoint_file_identity(rebound)
        ):
            raise ValueError(
                "timezone checkpoint evidence changed while reading"
            )
        for descriptor, component, parent in directories:
            revalidate_directory(descriptor, component, parent)
        return b"".join(chunks)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@dataclass(frozen=True)
class _RemainingStageResult:
    checkpoint: object
    query_batch_ids: tuple[str, ...]
    promoted: int
    fetched: int
    resumed: int
    quarantined: int
    pending: int
    network_attempts: int


def _materialize_remaining_split(
    root: Path,
    evidence_root: Path,
    parent_batch: object,
    parent_query: LogicalQuery,
    required: SplitRequired,
) -> tuple[object, object]:
    from .v52_query_plan import (
        build_statcast_split_batch,
        load_split_receipt,
        publish_query_batch,
        publish_split_receipt,
    )

    parent_id = logical_query_id(parent_query)
    child = _integrity_boundary(
        "REMAINING child batch",
        lambda: build_statcast_split_batch(root, parent_batch, parent_query),
    )
    _integrity_boundary(
        "REMAINING child batch publication",
        lambda: publish_query_batch(root, child),
    )
    receipt = _integrity_boundary(
        "REMAINING split receipt replay",
        lambda: load_split_receipt(root, parent_id, evidence_root=evidence_root),
    )
    if receipt is None:
        receipt = _integrity_boundary(
            "REMAINING split receipt publication",
            lambda: publish_split_receipt(
                root,
                evidence_root=evidence_root,
                parent_batch_id=parent_batch.batch_id,
                parent_query_id=parent_id,
                reason=required.reason,
                exhausted_attempt_number=required.exhausted_attempt_number,
                child_batch_id=child.batch_id,
            ),
        )
    if (
        receipt.parent_batch_id != parent_batch.batch_id
        or receipt.parent_query_id != parent_id
        or receipt.reason != required.reason
        or receipt.exhausted_attempt_number != required.exhausted_attempt_number
        or receipt.child_batch_id != child.batch_id
        or tuple(receipt.child_query_ids)
        != tuple(logical_query_id(query) for query in child.queries)
    ):
        raise TrustIntegrityError("REMAINING split receipt authority differs")
    return child, receipt


def _run_remaining_stage(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
    store: V52EvidenceStore,
    *,
    fixed_prefix: Sequence[object],
    participant_evidence: Sequence[Path],
    latest: object,
    evidence_root: Path,
    runner_guard: _RunnerGuard,
    disk_budget: _DiskBudget,
) -> _RemainingStageResult:
    from .execution_registry import publish_acquisition_checkpoint
    from .v52_legacy import load_protected_legacy_catalog
    from .v52_legacy_decision_inventory import (
        load_verified_legacy_decision_inventory,
        publish_legacy_decision_inventory,
    )
    from .v52_query_plan import (
        build_legacy_partition_batch,
        build_statcast_root_batch,
        load_query_batch,
        load_split_receipt,
        publish_query_batch,
    )

    prefix = tuple(fixed_prefix)
    if len(prefix) != 3:
        raise TrustIntegrityError("REMAINING fixed prefix differs")
    root = Path(os.path.abspath(os.fspath(request.root)))
    latest_stage = getattr(latest, "stage", None)
    if latest_stage not in {"PARTICIPANT_DERIVED", "REMAINING"}:
        raise TrustIntegrityError("REMAINING prior checkpoint differs")

    statcast_root = _integrity_boundary(
        "Statcast root batch",
        lambda: build_statcast_root_batch(root),
    )
    if (
        len(plan.query_batch_ids) != 3
        or statcast_root.batch_id != plan.query_batch_ids[1]
    ):
        raise TrustIntegrityError("Statcast root plan authority differs")
    if latest_stage == "REMAINING":
        persisted_root = _integrity_boundary(
            "Statcast root batch replay",
            lambda: load_query_batch(root, statcast_root.batch_id),
        )
        if persisted_root != statcast_root:
            raise TrustIntegrityError("persisted Statcast root batch differs")
    else:
        _integrity_boundary(
            "Statcast root batch publication",
            lambda: publish_query_batch(root, statcast_root),
        )

    inventory = _integrity_boundary(
        "legacy decision inventory replay"
        if latest_stage == "REMAINING"
        else "legacy decision inventory publication",
        lambda: (
            load_verified_legacy_decision_inventory(root)
            if latest_stage == "REMAINING"
            else publish_legacy_decision_inventory(root)
        ),
    )
    partition = _integrity_boundary(
        "legacy partition batch",
        lambda: build_legacy_partition_batch(root),
    )
    if latest_stage == "REMAINING":
        persisted_partition = _integrity_boundary(
            "legacy partition batch replay",
            lambda: load_query_batch(root, partition.batch_id),
        )
        if persisted_partition != partition:
            raise TrustIntegrityError("persisted legacy partition batch differs")
    else:
        _integrity_boundary(
            "legacy partition batch publication",
            lambda: publish_query_batch(root, partition),
        )

    protected = getattr(inventory, "payload", {}).get("protected_inventory")
    if not isinstance(protected, Mapping) or not isinstance(
        protected.get("sha256"), str
    ):
        raise TrustIntegrityError("protected legacy decision authority differs")
    catalog = _integrity_boundary(
        "protected legacy catalog",
        lambda: load_protected_legacy_catalog(
            root,
            legacy_root=root / _LEGACY_ROOT_RELATIVE,
            protected_inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
            protected_inventory_sha256=str(protected["sha256"]),
        ),
    )
    promoted_decisions = _validate_remaining_decisions(
        partition,
        inventory,
        catalog,
    )
    batches = [*prefix, partition]
    fixed_ids = tuple(batch.batch_id for batch in batches)
    split_ids: list[str] = []
    receipts: dict[str, object] = {}
    if latest_stage == "REMAINING":
        latest_batch_ids = getattr(latest, "query_batch_ids", None)
        latest_split_ids = getattr(latest, "split_query_ids", None)
        if (
            not isinstance(latest_batch_ids, tuple)
            or latest_batch_ids[:4] != fixed_ids
            or not isinstance(latest_split_ids, tuple)
            or latest_split_ids != tuple(sorted(latest_split_ids))
        ):
            raise TrustIntegrityError("REMAINING checkpoint authority differs")
        for batch_id in latest_batch_ids[4:]:
            batches.append(
                _integrity_boundary(
                    "REMAINING split batch replay",
                    lambda batch_id=batch_id: load_query_batch(root, batch_id),
                )
            )
        split_ids.extend(latest_split_ids)
        for parent_id in split_ids:
            receipt = _integrity_boundary(
                "REMAINING split receipt replay",
                lambda parent_id=parent_id: load_split_receipt(
                    root,
                    parent_id,
                    evidence_root=evidence_root,
                ),
            )
            if receipt is None:
                raise TrustIntegrityError("REMAINING split receipt is missing")
            receipts[parent_id] = receipt
        if {receipt.child_batch_id for receipt in receipts.values()} != {
            batch.batch_id for batch in batches[4:]
        }:
            raise TrustIntegrityError("REMAINING split DAG suffix differs")
    elif (
        getattr(latest, "query_batch_ids", None)
        != tuple(batch.batch_id for batch in prefix)
        or getattr(latest, "pending_query_ids", ())
        or getattr(latest, "quarantined_query_ids", ())
        or getattr(latest, "split_query_ids", ())
    ):
        raise TrustIntegrityError("terminal participant checkpoint differs")

    stage_evidence = _remaining_stage_evidence(
        root,
        participant_evidence,
        statcast_root.batch_id,
    )
    current = latest

    def populations() -> tuple[
        tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
    ]:
        return _remaining_populations(store, batches, split_ids)

    def publish_current() -> object:
        nonlocal current
        completed, pending, quarantined, split = populations()
        split_paths = tuple(
            sorted(
                (
                    root
                    / "audit/v5_2_qd/historical/acquisition/split_receipts"
                    / f"{parent_id}.json"
                    for parent_id in split
                ),
                key=os.fspath,
            )
        )
        current = _integrity_boundary(
            "REMAINING checkpoint",
            lambda: publish_acquisition_checkpoint(
                root,
                request.run_spec,
                expected_prior_checkpoint_sha256=getattr(
                    current, "receipt_sha256", None
                ),
                stage="REMAINING",
                query_batch_ids=tuple(batch.batch_id for batch in batches),
                completed_query_ids=completed,
                pending_query_ids=pending,
                quarantined_query_ids=quarantined,
                split_query_ids=split,
                stage_evidence_receipts=stage_evidence,
                split_receipt_paths=split_paths,
            ),
        )
        return current

    completed, pending, quarantined, split = populations()
    prefix_query_ids = {
        logical_query_id(query) for batch in prefix for query in batch.queries
    }
    if not prefix_query_ids <= set(completed):
        raise TrustIntegrityError("REMAINING prefix evidence is not terminal")
    if (
        latest_stage == "REMAINING"
        and not pending
        and not quarantined
        and getattr(latest, "completed_query_ids", None) == completed
        and getattr(latest, "pending_query_ids", None) == pending
        and getattr(latest, "quarantined_query_ids", None) == quarantined
        and getattr(latest, "split_query_ids", None) == split
        and getattr(latest, "query_batch_ids", None)
        == tuple(batch.batch_id for batch in batches)
    ):
        _validate_terminal_remaining_promotion(store, promoted_decisions)
        return _RemainingStageResult(
            latest,
            tuple(batch.batch_id for batch in batches),
            0,
            0,
            len(set(completed) - prefix_query_ids),
            0,
            0,
            0,
        )

    if latest_stage == "PARTICIPANT_DERIVED":
        publish_current()
    promoted_id = logical_query_id(promoted_decisions[0][0])
    if quarantined:
        newly_promoted = 0
        resumed_promoted = 0
    else:
        newly_promoted, resumed_promoted = _promote_remaining(
            store,
            catalog,
            promoted_decisions,
        )
        completed, pending, quarantined, _split = populations()
        if promoted_id not in completed:
            raise TrustIntegrityError("REMAINING promotion did not become terminal")
        if newly_promoted or promoted_id not in getattr(
            current, "completed_query_ids", ()
        ):
            publish_current()

    initially_completed = {
        query_id
        for query_id in completed
        if query_id not in prefix_query_ids and query_id != promoted_id
    }
    fetched = 0
    resumed = resumed_promoted + len(initially_completed)
    network_attempts = 0
    terminal_failure: TerminalAcquisitionFailure | None = None

    def batch_and_query(parent_id: str) -> tuple[object, LogicalQuery]:
        matches = [
            (batch, query)
            for batch in batches[3:]
            for query in batch.queries
            if logical_query_id(query) == parent_id
        ]
        if len(matches) != 1:
            raise TrustIntegrityError("REMAINING split parent binding differs")
        return matches[0]

    def repair_split(parent_id: str, required: SplitRequired) -> None:
        parent_batch, parent_query = batch_and_query(parent_id)
        child, receipt = _materialize_remaining_split(
            root,
            evidence_root,
            parent_batch,
            parent_query,
            required,
        )
        batches.append(child)
        split_ids.append(parent_id)
        split_ids.sort()
        receipts[parent_id] = receipt
        publish_current()

    while True:
        observed: list[tuple[str, SplitRequired]] = []
        active_queries = tuple(
            query
            for batch in batches[3:]
            for query in batch.queries
            if logical_query_id(query) not in split_ids
        )
        for query in active_queries:
            progress = _integrity_boundary(
                "REMAINING active-leaf replay",
                lambda query=query: store.read_verified_query_progress(query),
            )
            terminal = _terminal_progress(progress)
            if isinstance(terminal, SplitRequired):
                observed.append((logical_query_id(query), terminal))
        if observed:
            for parent_id, required in sorted(observed, key=lambda item: item[0]):
                repair_split(parent_id, required)
            continue

        completed, pending, quarantined, _split = populations()
        if quarantined:
            if tuple(quarantined) != getattr(current, "quarantined_query_ids", ()):
                publish_current()
            error = OperationalBlock(
                "REMAINING contains quarantined query evidence"
            )
            if terminal_failure is not None:
                raise error from terminal_failure
            raise error
        if terminal_failure is not None:
            raise TrustIntegrityError(
                "REMAINING terminal failure has no quarantined authority"
            ) from terminal_failure
        active_pending = tuple(
            query
            for query in active_queries
            if logical_query_id(query) in set(pending)
        )
        if not active_pending:
            if (
                getattr(current, "completed_query_ids", None) != completed
                or getattr(current, "pending_query_ids", None) != pending
            ):
                publish_current()
            return _RemainingStageResult(
                current,
                tuple(batch.batch_id for batch in batches),
                newly_promoted,
                fetched,
                resumed,
                0,
                0,
                network_attempts,
            )
        try:
            wave = _run_remaining_wave(
                request,
                plan,
                store,
                batches=batches,
                active_queries=active_pending,
                fetcher=request.fetcher,
                runner_guard=runner_guard,
                disk_budget=disk_budget,
            )
        except TerminalAcquisitionFailure as exc:
            terminal_failure = exc
            continue
        except OperationalBlock:
            publish_current()
            raise
        fetched += wave.fetched
        resumed += wave.resumed
        network_attempts += wave.network_attempts
        if wave.splits:
            continue
        publish_current()


def _validate_resumed_timezone_checkpoint(
    checkpoint: object,
    *,
    root: Path,
    batch_ids: tuple[str, str],
    completed_query_ids: tuple[str, ...],
    stage_evidence: tuple[Path, ...],
) -> None:
    supplied_paths = getattr(checkpoint, "stage_evidence_receipt_paths", None)
    expected_relative_paths = tuple(
        relative.as_posix() for relative in _TIMEZONE_STAGE_EVIDENCE_RELATIVES
    )
    expected_evidence = tuple(
        root / relative for relative in _TIMEZONE_STAGE_EVIDENCE_RELATIVES
    )
    if stage_evidence != expected_evidence:
        raise TrustIntegrityError("timezone checkpoint boundary differs")
    try:
        snapshots = tuple(
            _read_timezone_checkpoint_evidence_snapshot(
                root,
                path,
                max_bytes=max_bytes,
            )
            for path, max_bytes in zip(
                stage_evidence,
                _TIMEZONE_STAGE_EVIDENCE_LIMITS,
                strict=True,
            )
        )
        evidence_hashes = tuple(
            hashlib.sha256(snapshot).hexdigest() for snapshot in snapshots
        )
    except (OSError, ValueError) as exc:
        raise TrustIntegrityError(
            "timezone checkpoint evidence is unavailable"
        ) from exc
    if (
        tuple(sorted(stage_evidence, key=os.fspath)) != stage_evidence
        or getattr(checkpoint, "stage", None) != "TIMEZONE_REVIEW"
        or getattr(checkpoint, "query_batch_ids", None) != batch_ids
        or getattr(checkpoint, "completed_query_ids", None)
        != completed_query_ids
        or getattr(checkpoint, "pending_query_ids", None) != ()
        or getattr(checkpoint, "quarantined_query_ids", None) != ()
        or getattr(checkpoint, "split_query_ids", None) != ()
        or getattr(checkpoint, "split_receipt_sha256s", None) != ()
        or getattr(checkpoint, "stage_evidence_receipt_sha256s", None)
        != evidence_hashes
        or supplied_paths != expected_relative_paths
        or tuple(root / Path(path) for path in supplied_paths) != stage_evidence
    ):
        raise TrustIntegrityError("timezone checkpoint boundary differs")


def _real_run_stages(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
) -> AcquisitionSummary:
    from .execution_registry import (
        load_latest_acquisition_checkpoint,
        publish_acquisition_checkpoint,
    )
    from .v52_query_plan import (
        build_participant_derived_batch,
        build_schedule_reference_batch,
        build_static_batch,
        load_query_batch,
        publish_query_batch,
    )
    from .v52_schedule_inventory import (
        load_verified_schedule_anchor_inventory,
        publish_schedule_anchor_inventory,
    )

    if _real_authenticate(request) != plan:
        raise TrustIntegrityError("acquisition authority changed before stage execution")
    runner_guard = _current_runner_guard(request)
    root = Path(os.path.abspath(os.fspath(request.root)))
    evidence_root = _ensure_canonical_raw_parent(request)
    store = _integrity_boundary(
        "raw evidence store",
        lambda: V52EvidenceStore(evidence_root),
    )
    disk_budget = _DiskBudget(request)
    latest = _integrity_boundary(
        "acquisition checkpoint replay",
        lambda: load_latest_acquisition_checkpoint(root, request.run_spec),
    )
    if latest is not None and latest.stage not in {
        "STATIC_SCHEDULES",
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }:
        raise OperationalBlock("later acquisition stages are outside acquisition")

    static = _integrity_boundary(
        "static query batch",
        lambda: build_static_batch(
            root / _RUN_SPEC_RELATIVE,
            root / _ACQUISITION_CONTRACT_RELATIVE,
        ),
    )
    fetched = 0
    resumed = 0
    network_attempts = 0
    if latest is None or latest.stage == "STATIC_SCHEDULES":
        _integrity_boundary(
            "static query batch publication",
            lambda: publish_query_batch(root, static),
        )
        static_result = _execute_stage(
            request,
            plan,
            store,
            stage="STATIC_SCHEDULES",
            batches=(static,),
            stage_evidence=(),
            expected_prior=(
                None if latest is None else latest.receipt_sha256
            ),
            fetcher=request.fetcher,
            runner_guard=runner_guard,
            disk_budget=disk_budget,
        )
        latest = static_result.checkpoint
        fetched += static_result.fetched
        resumed += static_result.resumed
        network_attempts += static_result.network_attempts
    else:
        persisted_static = _integrity_boundary(
            "static query batch replay",
            lambda: load_query_batch(root, static.batch_id),
        )
        if persisted_static != static:
            raise TrustIntegrityError("persisted static batch authority differs")
        resumed += len(static.queries)

    if (
        latest is None
        or latest.stage
        not in {
            "STATIC_SCHEDULES",
            "REFERENCE_INPUTS",
            "TIMEZONE_REVIEW",
            "PARTICIPANT_DERIVED",
            "REMAINING",
        }
        or latest.stage == "STATIC_SCHEDULES"
        and (
            latest.pending_query_ids
            or latest.quarantined_query_ids
            or latest.split_query_ids
        )
    ):
        raise OperationalBlock("STATIC_SCHEDULES is not terminal")

    inventory_kwargs = {
        "acquisition_contract_path": root / _ACQUISITION_CONTRACT_RELATIVE,
        "static_batch_id": static.batch_id,
        "evidence_root": evidence_root,
    }
    if latest.stage in {
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }:
        inventory = _integrity_boundary(
            "schedule inventory replay",
            lambda: load_verified_schedule_anchor_inventory(
                root,
                request.run_spec,
                **inventory_kwargs,
            ),
        )
    else:
        inventory = _integrity_boundary(
            "schedule inventory publication",
            lambda: publish_schedule_anchor_inventory(
                root,
                request.run_spec,
                **inventory_kwargs,
            ),
        )
    _validate_schedule_inventory_schema(root, inventory)
    reference = _integrity_boundary(
        "reference query batch",
        lambda: build_schedule_reference_batch(
            root,
            root / _RUN_SPEC_RELATIVE,
            acquisition_contract_path=root / _ACQUISITION_CONTRACT_RELATIVE,
            static_batch_id=static.batch_id,
            evidence_root=evidence_root,
        ),
    )
    if reference.derived_from_sha256 != inventory.sha256:
        raise TrustIntegrityError("reference batch schedule inventory binding differs")

    if latest.stage in {
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }:
        persisted_reference = _integrity_boundary(
            "reference query batch replay",
            lambda: load_query_batch(root, reference.batch_id),
        )
        if persisted_reference != reference:
            raise TrustIntegrityError("persisted reference batch authority differs")
    else:
        _integrity_boundary(
            "reference query batch publication",
            lambda: publish_query_batch(root, reference),
        )

    cumulative_batches = (static, reference)
    cumulative_ids = tuple(
        sorted(logical_query_id(query) for batch in cumulative_batches for query in batch.queries)
    )
    terminal_reference = (
        latest.stage
        in {"REFERENCE_INPUTS", "TIMEZONE_REVIEW", "PARTICIPANT_DERIVED", "REMAINING"}
        and latest.query_batch_ids[:2] == (static.batch_id, reference.batch_id)
        and set(cumulative_ids).issubset(latest.completed_query_ids)
        and (
            latest.stage != "REFERENCE_INPUTS"
            or (
                latest.completed_query_ids == cumulative_ids
                and not latest.pending_query_ids
                and not latest.quarantined_query_ids
                and not latest.split_query_ids
            )
        )
    )
    if terminal_reference:
        completed, pending, quarantined = _stage_populations(store, cumulative_batches)
        if completed != cumulative_ids or pending or quarantined:
            raise TrustIntegrityError("terminal reference evidence replay differs")
        resumed += len(reference.queries)
    else:
        if latest.stage == "REFERENCE_INPUTS" and latest.query_batch_ids != (
            static.batch_id,
            reference.batch_id,
        ):
            raise TrustIntegrityError("reference checkpoint batch authority differs")
        reference_result = _execute_stage(
            request,
            plan,
            store,
            stage="REFERENCE_INPUTS",
            batches=cumulative_batches,
            active_batches=(reference,),
            stage_evidence=(inventory.path,),
            expected_prior=latest.receipt_sha256,
            fetcher=request.fetcher,
            runner_guard=runner_guard,
            disk_budget=disk_budget,
        )
        latest = reference_result.checkpoint
        fetched += reference_result.fetched
        resumed += reference_result.resumed
        network_attempts += reference_result.network_attempts

    reference_summary = AcquisitionSummary(
        contract_sha256=plan.contract_sha256,
        query_batch_sha256s=(static.batch_id, reference.batch_id),
        promoted=0,
        fetched=fetched,
        planned_promoted=plan.legacy_dispositions.count("PROMOTED"),
        planned_fetch_required=plan.legacy_dispositions.count("FETCH_REQUIRED"),
        resumed=resumed,
        quarantined=0,
        pending=0,
        network_attempts=network_attempts,
    )
    if request.stop_after == "reference-inputs":
        return reference_summary

    from .v52_timezone_review import publish_timezone_candidate

    _integrity_boundary(
        "timezone candidate publication",
        lambda: publish_timezone_candidate(
            root=root,
            evidence_root=evidence_root,
            batch_ids=(static.batch_id, reference.batch_id),
        ),
    )
    _load_required_timezone_gate(root)

    timezone_stage_evidence = (
        inventory.path,
        root / "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
        root / "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
        root / "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
        root / "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
        root / "config/v5_2_qd/venue_timezones.json",
    )
    if latest.stage == "REFERENCE_INPUTS":
        latest = _integrity_boundary(
            "timezone checkpoint",
            lambda: publish_acquisition_checkpoint(
                root,
                request.run_spec,
                expected_prior_checkpoint_sha256=latest.receipt_sha256,
                stage="TIMEZONE_REVIEW",
                query_batch_ids=(static.batch_id, reference.batch_id),
                completed_query_ids=cumulative_ids,
                pending_query_ids=(),
                quarantined_query_ids=(),
                split_query_ids=(),
                stage_evidence_receipts=timezone_stage_evidence,
            ),
        )
    if latest.stage == "TIMEZONE_REVIEW":
        _validate_resumed_timezone_checkpoint(
            latest,
            root=root,
            batch_ids=(static.batch_id, reference.batch_id),
            completed_query_ids=cumulative_ids,
            stage_evidence=timezone_stage_evidence,
        )
    elif (
        latest.stage not in {"PARTICIPANT_DERIVED", "REMAINING"}
        or latest.query_batch_ids[:2] != (static.batch_id, reference.batch_id)
        or not set(cumulative_ids).issubset(latest.completed_query_ids)
        or latest.stage == "PARTICIPANT_DERIVED"
        and (latest.quarantined_query_ids or latest.split_query_ids)
    ):
        raise TrustIntegrityError("timezone checkpoint authority differs")

    from .v52_game_evidence_inventory import (
        load_verified_game_evidence_inventory,
        publish_game_evidence_inventory,
    )

    if latest.stage in {"PARTICIPANT_DERIVED", "REMAINING"}:
        game_inventory = _integrity_boundary(
            "game evidence inventory replay",
            lambda: load_verified_game_evidence_inventory(root),
        )
    else:
        game_inventory = _integrity_boundary(
            "game evidence inventory publication",
            lambda: publish_game_evidence_inventory(root),
        )
    participant = _integrity_boundary(
        "participant query batch",
        lambda: build_participant_derived_batch(root),
    )
    if participant.derived_from_sha256 != game_inventory.sha256:
        raise TrustIntegrityError("participant batch inventory binding differs")
    if latest.stage in {"PARTICIPANT_DERIVED", "REMAINING"}:
        persisted_participant = _integrity_boundary(
            "participant query batch replay",
            lambda: load_query_batch(root, participant.batch_id),
        )
        if persisted_participant != participant:
            raise TrustIntegrityError("persisted participant batch authority differs")
    else:
        _integrity_boundary(
            "participant query batch publication",
            lambda: publish_query_batch(root, participant),
        )
    participant_batches = (static, reference, participant)
    participant_ids = tuple(
        sorted(
            logical_query_id(query)
            for batch in participant_batches
            for query in batch.queries
        )
    )
    participant_evidence = tuple(
        sorted((*timezone_stage_evidence, game_inventory.path), key=os.fspath)
    )
    terminal_participant = (
        latest.stage in {"PARTICIPANT_DERIVED", "REMAINING"}
        and latest.query_batch_ids[:3]
        == (static.batch_id, reference.batch_id, participant.batch_id)
        and set(participant_ids).issubset(latest.completed_query_ids)
        and (
            latest.stage != "PARTICIPANT_DERIVED"
            or (
                latest.completed_query_ids == participant_ids
                and not latest.pending_query_ids
                and not latest.quarantined_query_ids
                and not latest.split_query_ids
            )
        )
    )
    if terminal_participant:
        completed, pending, quarantined = _stage_populations(store, participant_batches)
        if completed != participant_ids or pending or quarantined:
            raise TrustIntegrityError("terminal participant evidence replay differs")
        resumed += len(participant.queries)
    else:
        participant_result = _execute_stage(
            request,
            plan,
            store,
            stage="PARTICIPANT_DERIVED",
            batches=participant_batches,
            active_batches=(participant,),
            stage_evidence=participant_evidence,
            expected_prior=latest.receipt_sha256,
            fetcher=request.fetcher,
            runner_guard=runner_guard,
            disk_budget=disk_budget,
        )
        fetched += participant_result.fetched
        resumed += participant_result.resumed
        network_attempts += participant_result.network_attempts
        latest = participant_result.checkpoint
    if latest.stage not in {"PARTICIPANT_DERIVED", "REMAINING"}:
        raise TrustIntegrityError("participant checkpoint authority differs")
    remaining = _run_remaining_stage(
        request,
        plan,
        store,
        fixed_prefix=participant_batches,
        participant_evidence=participant_evidence,
        latest=latest,
        evidence_root=evidence_root,
        runner_guard=runner_guard,
        disk_budget=disk_budget,
    )
    return AcquisitionSummary(
        contract_sha256=plan.contract_sha256,
        query_batch_sha256s=remaining.query_batch_ids,
        promoted=remaining.promoted,
        fetched=fetched + remaining.fetched,
        planned_promoted=plan.legacy_dispositions.count("PROMOTED"),
        planned_fetch_required=plan.legacy_dispositions.count("FETCH_REQUIRED"),
        resumed=resumed + remaining.resumed,
        quarantined=remaining.quarantined,
        pending=remaining.pending,
        network_attempts=network_attempts + remaining.network_attempts,
    )


def _real_inspect_runtime(
    request: _AcquisitionRequest,
    _plan: _AcquisitionPlan,
) -> _RuntimeState:
    from .execution_registry import load_execution_registry

    root = Path(os.path.abspath(os.fspath(request.root)))
    phase_root = root / "audit/v5_2_qd/historical/execution/phase_receipts"
    progress_paths = (
        root / "audit/v5_2_qd/historical/execution/acquisition_checkpoints",
        root / "audit/v5_2_qd/historical/acquisition/query_batches",
        root / "data/v5_2_qd/historical/raw",
    )
    has_progress = any(path.exists() for path in progress_paths)
    if not phase_root.exists():
        return _RuntimeState(None, has_progress)
    try:
        state = load_execution_registry(root, request.run_spec)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustIntegrityError("execution registry authentication failed") from exc
    return _RuntimeState(state.phase, True)


def _real_begin_runtime(
    request: _AcquisitionRequest,
    _plan: _AcquisitionPlan,
    runtime: _RuntimeState,
) -> None:
    from .execution_registry import (
        advance_execution_phase,
        initialize_execution_registry,
    )

    root = Path(os.path.abspath(os.fspath(request.root)))
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as exc:
        raise OperationalBlock("free-space reserve is unavailable") from exc
    if free_bytes < request.reserve_bytes:
        raise OperationalBlock("free-space reserve would be violated")
    try:
        phase = runtime.phase
        if phase is None:
            state = initialize_execution_registry(
                root,
                request.run_spec,
                design_path=root / _DESIGN_RELATIVE,
                plan_path=root / _PLAN_RELATIVE,
                acquisition_contract_path=root / _ACQUISITION_CONTRACT_RELATIVE,
            )
            phase = state.phase
        if phase == "DESIGN_ACCEPTED":
            advance_execution_phase(
                root,
                request.run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
                validation_receipts=(),
            )
        elif phase != "ACQUIRING":
            raise TrustIntegrityError("execution registry phase differs")
    except (OperationalBlock, TrustIntegrityError):
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustIntegrityError("execution registry initialization failed") from exc


def _real_replay_terminal(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
) -> AcquisitionSummary:
    from .execution_registry import load_terminal_acquisition_checkpoint
    from .v52_legacy import load_protected_legacy_catalog
    from .v52_legacy_decision_inventory import (
        load_verified_legacy_decision_inventory,
    )
    from .v52_query_plan import load_query_batch

    root = Path(os.path.abspath(os.fspath(request.root)))
    try:
        checkpoint = load_terminal_acquisition_checkpoint(root, request.run_spec)
        batch_ids = checkpoint.query_batch_ids
        if (
            checkpoint.stage != "REMAINING"
            or checkpoint.pending_query_ids
            or checkpoint.quarantined_query_ids
            or len(checkpoint.stage_evidence_receipt_sha256s) != 10
            or len(batch_ids) < 4
            or batch_ids[0] != plan.query_batch_ids[0]
        ):
            raise TrustIntegrityError(
                "SOURCES_READY terminal checkpoint authority differs"
            )
        partition = load_query_batch(
            root,
            batch_ids[3],
            read_only_progress=True,
        )
        inventory = load_verified_legacy_decision_inventory(root)
        protected = inventory.payload.get("protected_inventory")
        if not isinstance(protected, Mapping) or not isinstance(
            protected.get("sha256"), str
        ):
            raise TrustIntegrityError(
                "SOURCES_READY protected decision authority differs"
            )
        catalog = load_protected_legacy_catalog(
            root,
            legacy_root=root / _LEGACY_ROOT_RELATIVE,
            protected_inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
            protected_inventory_sha256=str(protected["sha256"]),
        )
        promoted = _validate_remaining_decisions(partition, inventory, catalog)
        evidence_root = root / "data/v5_2_qd/historical/raw"
        if not evidence_root.is_dir():
            raise TrustIntegrityError(
                "SOURCES_READY raw evidence authority is unavailable"
            )
        store = V52EvidenceStore.open_read_only(evidence_root)
        _validate_terminal_remaining_promotion(store, promoted)
        planned_promoted = plan.legacy_dispositions.count("PROMOTED")
        planned_fetch = plan.legacy_dispositions.count("FETCH_REQUIRED")
        if planned_promoted != 1 or planned_fetch != 47:
            raise TrustIntegrityError(
                "SOURCES_READY planned disposition authority differs"
            )
        return AcquisitionSummary(
            contract_sha256=plan.contract_sha256,
            query_batch_sha256s=batch_ids,
            promoted=0,
            fetched=0,
            planned_promoted=planned_promoted,
            planned_fetch_required=planned_fetch,
            resumed=len(checkpoint.completed_query_ids),
            quarantined=0,
            pending=0,
            network_attempts=0,
        )
    except TrustIntegrityError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TrustIntegrityError(
            "SOURCES_READY terminal replay authentication failed"
        ) from exc


_REAL_OPS = _AcquireOps(
    authenticate=_real_authenticate,
    runner_lock=_real_runner_lock,
    inspect_runtime=_real_inspect_runtime,
    begin_runtime=_real_begin_runtime,
    replay_terminal=_real_replay_terminal,
    run_stages=_real_run_stages,
)


def acquire_sources(
    root: Path,
    run_spec: RunSpec | SourceRunSpec,
    *,
    years: range,
    mode: str,
    resume: bool,
    stop_after: str | None,
    legacy_root: Path,
    protected_inventory_path: Path,
    reserve_bytes: int = _MINIMUM_RESERVE_BYTES,
    fetcher: Callable[..., AttemptCapture] = fetch_attempt,
    plan_only: bool = False,
    acquisition_contract_path: Path | None = None,
) -> AcquisitionSummary:
    if not callable(fetcher):
        raise TypeError("fetcher must be callable")
    request = _AcquisitionRequest(
        root=Path(root),
        run_spec=run_spec,
        years=years,
        mode=mode,
        resume=resume,
        stop_after=stop_after,
        legacy_root=Path(legacy_root),
        protected_inventory_path=Path(protected_inventory_path),
        reserve_bytes=reserve_bytes,
        plan_only=plan_only,
        fetcher=fetcher,
        acquisition_contract_path=acquisition_contract_path,
    )
    return _run_acquisition(request, _REAL_OPS)


def _parse_years(value: str) -> range:
    if value != "2018:2024":
        raise argparse.ArgumentTypeError("years must be exactly 2018:2024")
    return range(2018, 2025)


def _positive_reserve(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("reserve bytes must be an integer") from exc
    if parsed < _MINIMUM_RESERVE_BYTES:
        raise argparse.ArgumentTypeError("reserve bytes must be at least 20 GiB")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m mlb_backtest.v52_acquire")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--acquisition-contract", type=Path)
    parser.add_argument("--years", type=_parse_years, required=True)
    parser.add_argument("--mode", choices=("hybrid",), required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--protected-inventory", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--stop-after", choices=("reference-inputs",), default=None)
    parser.add_argument(
        "--reserve-bytes",
        type=_positive_reserve,
        default=_MINIMUM_RESERVE_BYTES,
    )
    return parser


def _cli_exact_run_spec(root: Path, supplied: Path) -> Path:
    canonical_root = Path(os.path.abspath(os.fspath(root)))
    candidate = _absolute_supplied(supplied, canonical_root)
    permitted = {
        canonical_root / _RUN_SPEC_RELATIVE,
        canonical_root
        / "protocols/v5_2_qd/"
        "historical_walkforward_500_superseding_565534.json",
    }
    if candidate not in permitted:
        raise TrustIntegrityError("run spec path differs from the frozen contract")
    try:
        linked = candidate.lstat()
    except OSError as exc:
        raise TrustIntegrityError("run spec is unavailable") from exc
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise TrustIntegrityError("run spec path identity differs")
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.plan_only and args.resume:
        _parser().error("--plan-only and --resume are mutually exclusive")
    try:
        run_spec_path = _cli_exact_run_spec(args.root, args.run_spec)
        try:
            run_spec = (
                load_source_run_spec(run_spec_path)
                if run_spec_path.name.endswith("_superseding_565534.json")
                else load_run_spec(run_spec_path)
            )
        except (OSError, ValueError) as exc:
            raise TrustIntegrityError("run spec trust failed") from exc
        summary = acquire_sources(
            args.root,
            run_spec,
            years=args.years,
            mode=args.mode,
            resume=args.resume,
            stop_after=args.stop_after,
            legacy_root=args.legacy_root,
            protected_inventory_path=args.protected_inventory,
            reserve_bytes=args.reserve_bytes,
            plan_only=args.plan_only,
            acquisition_contract_path=(
                args.acquisition_contract
                if args.acquisition_contract is not None
                else args.root / _ACQUISITION_CONTRACT_RELATIVE
            ),
        )
    except KeyboardInterrupt:
        return 130
    except OperationalBlock as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TrustIntegrityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "contract_sha256": summary.contract_sha256,
                "query_batch_sha256s": list(summary.query_batch_sha256s),
                "promoted": summary.promoted,
                "fetched": summary.fetched,
                "planned_promoted": summary.planned_promoted,
                "planned_fetch_required": summary.planned_fetch_required,
                "resumed": summary.resumed,
                "quarantined": summary.quarantined,
                "pending": summary.pending,
                "network_attempts": summary.network_attempts,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "AcquisitionSummary",
    "OperationalBlock",
    "SplitRequired",
    "TerminalAcquisitionFailure",
    "TrustIntegrityError",
    "acquire_sources",
    "acquire_with_retries",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
