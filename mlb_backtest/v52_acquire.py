"""Resumable single-query acquisition with frozen retry semantics."""

from __future__ import annotations

import time
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Literal

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


__all__ = [
    "SplitRequired",
    "TerminalAcquisitionFailure",
    "acquire_with_retries",
]
