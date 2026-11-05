"""Message handling decisions, independent of RabbitMQ so it can be unit-tested.

Delivery is at-least-once; correctness comes from the guarded state transitions in the store:
a duplicate delivery of a finished execution is acknowledged without running anything, and
a result can only be written while the row is still RUNNING (idempotent result recording).
This is NOT exactly-once *execution*: if a worker dies mid-run, the job may run again.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from enum import Enum

from codeforge_shared import metrics
from codeforge_shared.config import Settings
from codeforge_shared.logging_utils import log_event
from codeforge_shared.results import ClaimKind, RunResult
from codeforge_shared.retry import RetryPolicy
from codeforge_shared.states import ErrorType, ExecutionStatus

from .runner import SandboxInfrastructureError

try:
    from sqlalchemy.exc import SQLAlchemyError
    TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, ConnectionError)
except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency at runtime
    TRANSIENT_DB_ERRORS = (ConnectionError,)

log = logging.getLogger(__name__)


class Decision(str, Enum):
    ACK = "ack"
    RETRY = "retry"  # re-publish to the delayed retry queue (bounded), then ack the original
    DEAD = "dead"  # poison message: publish to the dead-letter queue, then ack the original


class Processor:
    def __init__(self, store, runner, settings: Settings, worker_id: str, sleep=time.sleep):
        self.store = store
        self.runner = runner
        self.settings = settings
        self.worker_id = worker_id
        self._record_policy = RetryPolicy(max_attempts=3, base_delay=0.5)
        self._sleep = sleep

    def process(self, body: bytes) -> Decision:
        try:
            msg = json.loads(body)
            execution_id = uuid.UUID(str(msg["execution_id"]))
        except (ValueError, KeyError, TypeError):
            log_event(log, logging.ERROR, "poison_message", reason="malformed", size=len(body))
            metrics.MESSAGES.labels("dead_malformed").inc()
            return Decision.DEAD
        corr = str(msg.get("correlation_id", ""))[:64]

        try:
            claim = self.store.claim(execution_id, self.worker_id)
        except TRANSIENT_DB_ERRORS:
            log_event(log, logging.WARNING, "db_unavailable_on_claim",
                      execution_id=str(execution_id), correlation_id=corr, exc_info=True)
            metrics.MESSAGES.labels("retry_db").inc()
            return Decision.RETRY

        if claim.kind is ClaimKind.NOT_FOUND:
            log_event(log, logging.ERROR, "poison_message", reason="unknown_execution",
                      execution_id=str(execution_id))
            metrics.MESSAGES.labels("dead_unknown").inc()
            return Decision.DEAD
        if claim.kind is ClaimKind.TERMINAL:
            log_event(log, logging.INFO, "duplicate_delivery_ignored",
                      execution_id=str(execution_id), correlation_id=corr)
            metrics.MESSAGES.labels("duplicate_terminal").inc()
            return Decision.ACK
        if claim.kind is ClaimKind.IN_PROGRESS:
            # Another worker owns it, or a crashed worker left it RUNNING. The sweeper requeues
            # stale RUNNING rows with a fresh outbox event, so acking here cannot lose the job.
            log_event(log, logging.INFO, "duplicate_delivery_in_progress",
                      execution_id=str(execution_id), correlation_id=corr)
            metrics.MESSAGES.labels("duplicate_in_progress").inc()
            return Decision.ACK
        if claim.kind is ClaimKind.QUEUED:
            return Decision.RETRY

        run_input = claim.run_input
        log_event(log, logging.INFO, "execution_started", execution_id=str(execution_id),
                  correlation_id=corr, attempt=run_input.attempt_number, language=run_input.language)
        metrics.WORKER_ACTIVE.inc()
        try:
            result = self.runner.run(
                run_input, cancel_check=lambda: self.store.is_cancel_requested(execution_id),
                execution_id=str(execution_id))
        except Exception as exc:  # noqa: BLE001
            infra = isinstance(exc, SandboxInfrastructureError)
            log_event(log, logging.ERROR, "runner_failed", execution_id=str(execution_id),
                      correlation_id=corr, infra=infra, exc_info=True)
            metrics.QUEUE_FAILURES.labels("runner").inc()
            return self._handle_runner_failure(execution_id, run_input.attempt_number, str(exc))
        finally:
            metrics.WORKER_ACTIVE.dec()

        return self._record(execution_id, run_input.language, result, corr)

    # ------------------------------------------------------------------
    def _record(self, execution_id, language, result: RunResult, corr: str) -> Decision:
        for attempt in range(1, self._record_policy.max_attempts + 1):
            try:
                written = self.store.finish(execution_id, result)
                break
            except TRANSIENT_DB_ERRORS:
                log_event(log, logging.WARNING, "db_unavailable_on_record",
                          execution_id=str(execution_id), attempt=attempt, exc_info=True)
                if not self._record_policy.should_retry(attempt):
                    # Result lost; row stays RUNNING and the sweeper will requeue it later.
                    metrics.MESSAGES.labels("record_failed").inc()
                    return Decision.RETRY
                self._sleep(self._record_policy.delay_for(attempt))
        if not written:
            log_event(log, logging.WARNING, "stale_result_discarded", execution_id=str(execution_id),
                      correlation_id=corr)
            metrics.MESSAGES.labels("stale_result").inc()
            return Decision.ACK
        et = result.error_type.value if result.error_type else ""
        metrics.EXECUTIONS.labels(language, result.status.value, et).inc()
        metrics.EXECUTION_DURATION.labels(language).observe(result.duration_ms / 1000)
        metrics.MESSAGES.labels("processed").inc()
        log_event(log, logging.INFO, "execution_finished", execution_id=str(execution_id),
                  correlation_id=corr, status=result.status.value, exit_code=result.exit_code,
                  duration_ms=result.duration_ms, error_type=et or None)
        return Decision.ACK

    def _handle_runner_failure(self, execution_id, attempt_number: int, detail: str) -> Decision:
        try:
            if attempt_number < self.settings.max_attempts:
                # New outbox event => a fresh message is delivered after a delay; ack the old one.
                self.store.requeue(execution_id, ErrorType.INFRASTRUCTURE_ERROR, detail[:200],
                                   delay_seconds=self.settings.retry_delay_ms / 1000)
                metrics.MESSAGES.labels("requeued_infra").inc()
                return Decision.ACK
            failed = RunResult(ExecutionStatus.FAILED, None, "", "Execution infrastructure error.",
                               0, ErrorType.INFRASTRUCTURE_ERROR)
            self.store.finish(execution_id, failed)
            metrics.MESSAGES.labels("failed_infra").inc()
            return Decision.ACK
        except TRANSIENT_DB_ERRORS:
            return Decision.RETRY
