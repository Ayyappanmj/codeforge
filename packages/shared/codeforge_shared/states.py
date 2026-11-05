"""Execution lifecycle: statuses, allowed transitions, error categories."""
from __future__ import annotations

from enum import Enum


class ExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ErrorType(str, Enum):
    RUNTIME_ERROR = "RUNTIME_ERROR"  # non-zero exit code
    TIMEOUT = "TIMEOUT"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    MEMORY_LIMIT_EXCEEDED = "MEMORY_LIMIT_EXCEEDED"
    CANCELLED = "CANCELLED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"  # docker/db problems, not user's fault
    WORKER_LOST = "WORKER_LOST"  # worker died mid-run and attempts were exhausted
    QUEUE_EXHAUSTED = "QUEUE_EXHAUSTED"  # message could never be delivered/processed


S = ExecutionStatus
TERMINAL_STATUSES = frozenset({S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLED})

ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLED, S.FAILED}),  # FAILED: queue exhausted
    S.RUNNING: frozenset(
        {S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLED, S.QUEUED}  # QUEUED: recovery
    ),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.TIMED_OUT: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def is_terminal(status: ExecutionStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: ExecutionStatus, target: ExecutionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} is not allowed")


def sources_for(target: ExecutionStatus) -> frozenset[ExecutionStatus]:
    """Statuses from which `target` may be reached (used to build guarded UPDATEs)."""
    return frozenset(s for s, targets in ALLOWED_TRANSITIONS.items() if target in targets)
