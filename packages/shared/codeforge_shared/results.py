from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .states import ErrorType, ExecutionStatus


@dataclass(frozen=True)
class RunResult:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    error_type: ErrorType | None = None
    output_truncated: bool = False


@dataclass(frozen=True)
class RunInput:
    language: str
    source_code: str
    stdin: str
    attempt_number: int


class ClaimKind(str, Enum):
    CLAIMED = "claimed"
    TERMINAL = "terminal"  # duplicate delivery of a finished execution
    IN_PROGRESS = "in_progress"  # someone else is running it
    QUEUED = "queued"  # not claimable right now (should not normally happen)
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ClaimResult:
    kind: ClaimKind
    run_input: RunInput | None = None


class CancelOutcome(str, Enum):
    CANCELLED = "cancelled"  # was queued; cancelled immediately
    CANCEL_REQUESTED = "cancel_requested"  # running; worker will kill the container
    NOT_CANCELLABLE = "not_cancellable"  # already terminal
    NOT_FOUND = "not_found"
