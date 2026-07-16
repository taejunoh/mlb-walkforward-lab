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

from .protocols import RunSpec, load_run_spec
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
    run_spec: RunSpec
    years: range
    mode: str
    resume: bool
    stop_after: str | None
    legacy_root: Path
    protected_inventory_path: Path
    reserve_bytes: int
    plan_only: bool
    fetcher: Callable[..., AttemptCapture] = fetch_attempt


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


@dataclass(frozen=True)
class _AcquireOps:
    authenticate: Callable[[_AcquisitionRequest], _AcquisitionPlan]
    runner_lock: Callable[[_AcquisitionRequest], ContextManager[None]]
    inspect_runtime: Callable[[_AcquisitionRequest, _AcquisitionPlan], _RuntimeState]
    begin_runtime: Callable[
        [_AcquisitionRequest, _AcquisitionPlan, _RuntimeState], None
    ]
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

    with ops.runner_lock(request):
        runtime = ops.inspect_runtime(request, plan)
        if not isinstance(runtime, _RuntimeState):
            raise TypeError("runtime inspection must return a _RuntimeState")
        if runtime.has_progress and not request.resume:
            raise OperationalBlock("existing acquisition progress requires --resume")
        if runtime.phase in {
            "SOURCES_READY",
            "OOF_SEALED",
            "CARDS_SEALED",
            "TARGET_LOCKED",
            "OUTCOMES_JOINED",
            "SAMPLE_500_SEALED",
            "VERIFIED",
        }:
            raise OperationalBlock(
                f"execution phase {runtime.phase} does not permit acquisition"
            )
        if runtime.phase not in {None, "DESIGN_ACCEPTED", "ACQUIRING"}:
            raise TrustIntegrityError("execution phase authority differs")
        ops.begin_runtime(request, plan, runtime)
        summary = ops.run_stages(request, plan)
        if not isinstance(summary, AcquisitionSummary):
            raise TypeError("stage runner must return an AcquisitionSummary")
        return summary


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
    if not isinstance(request.run_spec, RunSpec):
        raise TrustIntegrityError("run_spec must be a RunSpec")
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

    run_path = _require_exact_path(
        root, root / _RUN_SPEC_RELATIVE, _RUN_SPEC_RELATIVE, "run spec"
    )
    contract_path = _require_exact_path(
        root,
        root / _ACQUISITION_CONTRACT_RELATIVE,
        _ACQUISITION_CONTRACT_RELATIVE,
        "acquisition contract",
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


def _real_run_stages(
    request: _AcquisitionRequest,
    plan: _AcquisitionPlan,
) -> AcquisitionSummary:
    from .execution_registry import load_latest_acquisition_checkpoint
    from .v52_query_plan import (
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
    }:
        raise OperationalBlock("later acquisition stages are outside C2b")

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
        or latest.stage not in {"STATIC_SCHEDULES", "REFERENCE_INPUTS"}
        or latest.pending_query_ids
        and latest.stage == "STATIC_SCHEDULES"
        or latest.quarantined_query_ids
        or latest.split_query_ids
    ):
        raise OperationalBlock("STATIC_SCHEDULES is not terminal")

    inventory_kwargs = {
        "acquisition_contract_path": root / _ACQUISITION_CONTRACT_RELATIVE,
        "static_batch_id": static.batch_id,
        "evidence_root": evidence_root,
    }
    if latest.stage == "REFERENCE_INPUTS":
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

    if latest.stage == "REFERENCE_INPUTS":
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
        latest.stage == "REFERENCE_INPUTS"
        and latest.query_batch_ids == (static.batch_id, reference.batch_id)
        and latest.completed_query_ids == cumulative_ids
        and not latest.pending_query_ids
        and not latest.quarantined_query_ids
        and not latest.split_query_ids
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

    if request.stop_after != "reference-inputs":
        raise OperationalBlock("TIMEZONE_REVIEW orchestration is pending")
    return AcquisitionSummary(
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


_REAL_OPS = _AcquireOps(
    authenticate=_real_authenticate,
    runner_lock=_real_runner_lock,
    inspect_runtime=_real_inspect_runtime,
    begin_runtime=_real_begin_runtime,
    run_stages=_real_run_stages,
)


def acquire_sources(
    root: Path,
    run_spec: RunSpec,
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
    expected = canonical_root / _RUN_SPEC_RELATIVE
    if candidate != expected:
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
            run_spec = load_run_spec(run_spec_path)
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
