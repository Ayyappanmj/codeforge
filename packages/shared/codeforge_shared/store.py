"""All database access. Every state change is a single guarded UPDATE (compare-and-set on the
status column), so concurrent workers/API calls cannot overwrite each other, and no transaction
is ever held open while user code runs."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine, exists, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from .models import Execution, ExecutionAttempt, OutboxEvent, utcnow
from .results import CancelOutcome, ClaimKind, ClaimResult, RunInput, RunResult
from .states import (
    ErrorType, ExecutionStatus, TERMINAL_STATUSES, assert_transition,
)

S = ExecutionStatus
TERMINAL_VALUES = [s.value for s in TERMINAL_STATUSES]


def make_session_factory(database_url: str, **engine_kwargs) -> sessionmaker[Session]:
    engine = create_engine(database_url, pool_pre_ping=True, **engine_kwargs)
    return sessionmaker(engine, expire_on_commit=False)


class SqlStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory

    @contextmanager
    def _session(self):
        with self._sf() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    # ---------- health ----------
    def ping(self) -> None:
        with self._sf() as session:
            session.execute(text("SELECT 1"))

    # ---------- API side ----------
    def create_execution(self, *, language, source_code, stdin, correlation_id) -> Execution:
        """Execution row + outbox row in ONE transaction (transactional outbox)."""
        now = utcnow()
        ex = Execution(
            id=uuid.uuid4(), language=language, source_code=source_code, stdin=stdin,
            status=S.QUEUED.value, created_at=now, updated_at=now,
            correlation_id=correlation_id, enqueue_count=1,
        )
        with self._session() as s:
            s.add(ex)
            s.flush()  # execution row must exist before the outbox FK row
            s.add(OutboxEvent(execution_id=ex.id, correlation_id=correlation_id, created_at=now,
                              next_attempt_at=now))
        return ex

    def get_execution(self, execution_id: uuid.UUID) -> Execution | None:
        with self._sf() as s:
            return s.get(Execution, execution_id)

    def list_executions(self, *, page: int, page_size: int, status: str | None = None,
                        language: str | None = None) -> tuple[list[Execution], int]:
        conds = []
        if status:
            conds.append(Execution.status == status)
        if language:
            conds.append(Execution.language == language)
        with self._sf() as s:
            total = s.scalar(select(func.count()).select_from(Execution).where(*conds)) or 0
            rows = s.scalars(
                select(Execution).where(*conds)
                .order_by(Execution.created_at.desc(), Execution.id)
                .limit(page_size).offset((page - 1) * page_size)
            ).all()
            return list(rows), total

    def request_cancel(self, execution_id: uuid.UUID) -> CancelOutcome:
        assert_transition(S.QUEUED, S.CANCELLED)
        now = utcnow()
        with self._session() as s:
            r = s.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == S.QUEUED.value)
                .values(status=S.CANCELLED.value, completed_at=now, updated_at=now,
                        cancel_requested=True, error_type=ErrorType.CANCELLED.value)
            )
            if r.rowcount == 1:
                return CancelOutcome.CANCELLED
            r = s.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == S.RUNNING.value)
                .values(cancel_requested=True, updated_at=now)
            )
            if r.rowcount == 1:
                return CancelOutcome.CANCEL_REQUESTED
            exists_ = s.scalar(select(Execution.id).where(Execution.id == execution_id))
            return CancelOutcome.NOT_CANCELLABLE if exists_ else CancelOutcome.NOT_FOUND

    # ---------- worker side ----------
    def claim(self, execution_id: uuid.UUID, worker_id: str) -> ClaimResult:
        """QUEUED -> RUNNING compare-and-set. Exactly one concurrent caller can win."""
        assert_transition(S.QUEUED, S.RUNNING)
        now = utcnow()
        with self._session() as s:
            r = s.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == S.QUEUED.value)
                .values(status=S.RUNNING.value, started_at=now, updated_at=now,
                        worker_id=worker_id, attempt_count=Execution.attempt_count + 1,
                        completed_at=None, error_type=None)
            )
            if r.rowcount == 1:
                ex = s.get(Execution, execution_id, populate_existing=True)
                s.add(ExecutionAttempt(execution_id=execution_id, attempt_number=ex.attempt_count,
                                       worker_id=worker_id, started_at=now))
                return ClaimResult(ClaimKind.CLAIMED, RunInput(
                    ex.language, ex.source_code, ex.stdin, ex.attempt_count))
            status = s.scalar(select(Execution.status).where(Execution.id == execution_id))
            if status is None:
                return ClaimResult(ClaimKind.NOT_FOUND)
            if status in TERMINAL_VALUES:
                return ClaimResult(ClaimKind.TERMINAL)
            if status == S.RUNNING.value:
                return ClaimResult(ClaimKind.IN_PROGRESS)
            return ClaimResult(ClaimKind.QUEUED)

    def finish(self, execution_id: uuid.UUID, result: RunResult) -> bool:
        """RUNNING -> terminal, only if still RUNNING. Returns False for stale/duplicate writes
        (idempotent result recording: a finished execution is never overwritten)."""
        assert_transition(S.RUNNING, result.status)
        now = utcnow()
        with self._session() as s:
            r = s.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == S.RUNNING.value)
                .values(status=result.status.value, completed_at=now, updated_at=now,
                        exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                        output_truncated=result.output_truncated,
                        execution_duration_ms=result.duration_ms,
                        error_type=result.error_type.value if result.error_type else None)
            )
            if r.rowcount != 1:
                return False
            self._close_attempt(s, execution_id, result.status.value,
                                result.error_type.value if result.error_type else None,
                                result.duration_ms, now)
            return True

    def requeue(self, execution_id: uuid.UUID, error: ErrorType = ErrorType.INFRASTRUCTURE_ERROR,
                reason: str = "", delay_seconds: float = 0) -> bool:
        """RUNNING -> QUEUED with a fresh outbox event (recoverable infrastructure failure)."""
        assert_transition(S.RUNNING, S.QUEUED)
        now = utcnow()
        with self._session() as s:
            return self._requeue(s, execution_id, now, [S.RUNNING.value], error, reason,
                                 delay_seconds=delay_seconds)

    def is_cancel_requested(self, execution_id: uuid.UUID) -> bool:
        with self._sf() as s:
            return bool(s.scalar(select(Execution.cancel_requested).where(Execution.id == execution_id)))

    def get_status(self, execution_id: uuid.UUID) -> str | None:
        with self._sf() as s:
            return s.scalar(select(Execution.status).where(Execution.id == execution_id))

    # ---------- outbox ----------
    def publish_due_outbox(self, publish_fn, batch_size: int, now: datetime | None = None) -> int:
        """Lock a batch (SKIP LOCKED on PostgreSQL), publish each, mark published. If the process
        dies after publish but before commit, the event is re-published: consumers are idempotent."""
        now = now or utcnow()
        published = 0
        failure: Exception | None = None
        with self._session() as s:
            events = s.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None), OutboxEvent.next_attempt_at <= now)
                .order_by(OutboxEvent.created_at).limit(batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            for ev in events:
                try:
                    publish_fn(str(ev.execution_id), ev.correlation_id)
                except Exception as exc:  # noqa: BLE001 - any broker failure => backoff & retry
                    ev.publish_attempts += 1
                    ev.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    ev.next_attempt_at = now + timedelta(seconds=min(60, 2 ** ev.publish_attempts))
                    failure = exc
                    break  # broker is probably unhealthy; commit progress so far, then re-raise
                ev.published_at = utcnow()
                ev.publish_attempts += 1
                published += 1
        if failure is not None:
            raise failure
        return published

    def pending_outbox_count(self) -> int:
        with self._sf() as s:
            return s.scalar(select(func.count()).select_from(OutboxEvent)
                            .where(OutboxEvent.published_at.is_(None))) or 0

    # ---------- recovery sweeper ----------
    def sweep(self, *, stale_running_seconds: float, stale_queued_seconds: float,
              max_attempts: int, max_enqueues: int, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        counts = {"requeued_running": 0, "failed_running": 0, "requeued_queued": 0,
                  "failed_queued": 0}
        running_cutoff = now - timedelta(seconds=stale_running_seconds)
        queued_cutoff = now - timedelta(seconds=stale_queued_seconds)

        with self._sf() as s:
            stale_running = s.execute(
                select(Execution.id, Execution.attempt_count).where(
                    Execution.status == S.RUNNING.value, Execution.started_at < running_cutoff)
            ).all()
            stale_queued = s.execute(
                select(Execution.id, Execution.enqueue_count).where(
                    Execution.status == S.QUEUED.value, Execution.updated_at < queued_cutoff,
                    ~exists().where(OutboxEvent.execution_id == Execution.id,
                                    OutboxEvent.published_at.is_(None)))
            ).all()

        for ex_id, attempts in stale_running:
            with self._session() as s:
                if attempts < max_attempts:
                    if self._requeue(s, ex_id, now, [S.RUNNING.value], ErrorType.WORKER_LOST,
                                     "worker lost", only_if_started_before=running_cutoff):
                        counts["requeued_running"] += 1
                else:
                    r = s.execute(
                        update(Execution)
                        .where(Execution.id == ex_id, Execution.status == S.RUNNING.value,
                               Execution.started_at < running_cutoff)
                        .values(status=S.FAILED.value, error_type=ErrorType.WORKER_LOST.value,
                                completed_at=now, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if r.rowcount == 1:
                        self._close_attempt(s, ex_id, S.FAILED.value, ErrorType.WORKER_LOST.value,
                                            None, now)
                        counts["failed_running"] += 1

        for ex_id, enqueues in stale_queued:
            with self._session() as s:
                if enqueues < max_enqueues:
                    ex = s.get(Execution, ex_id)
                    r = s.execute(
                        update(Execution)
                        .where(Execution.id == ex_id, Execution.status == S.QUEUED.value,
                               Execution.updated_at < queued_cutoff)
                        .values(enqueue_count=Execution.enqueue_count + 1, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if r.rowcount == 1 and ex is not None:
                        s.add(OutboxEvent(execution_id=ex_id, correlation_id=ex.correlation_id,
                                          created_at=now, next_attempt_at=now))
                        counts["requeued_queued"] += 1
                else:
                    r = s.execute(
                        update(Execution)
                        .where(Execution.id == ex_id, Execution.status == S.QUEUED.value,
                               Execution.updated_at < queued_cutoff)
                        .values(status=S.FAILED.value, error_type=ErrorType.QUEUE_EXHAUSTED.value,
                                completed_at=now, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if r.rowcount == 1:
                        counts["failed_queued"] += 1
        return counts

    # ---------- helpers ----------
    @staticmethod
    def _close_attempt(s: Session, execution_id, outcome, error_type, duration_ms, now) -> None:
        attempt_id = s.scalar(
            select(ExecutionAttempt.id).where(ExecutionAttempt.execution_id == execution_id,
                                           ExecutionAttempt.finished_at.is_(None))
            .order_by(ExecutionAttempt.attempt_number.desc()).limit(1)
        )
        if attempt_id is not None:
            s.execute(
                update(ExecutionAttempt)
                .where(ExecutionAttempt.id == attempt_id, ExecutionAttempt.finished_at.is_(None))
                .values(finished_at=now, outcome=outcome, error_type=error_type, duration_ms=duration_ms)
                .execution_options(synchronize_session=False)
            )

    def _requeue(self, s: Session, execution_id, now, from_statuses, error, reason,
                 only_if_started_before: datetime | None = None, delay_seconds: float = 0) -> bool:
        conds = [Execution.id == execution_id, Execution.status.in_(from_statuses)]
        if only_if_started_before is not None:
            conds.append(Execution.started_at < only_if_started_before)
        r = s.execute(
            update(Execution).where(*conds)
            .values(status=S.QUEUED.value, updated_at=now, started_at=None, worker_id=None,
                    enqueue_count=Execution.enqueue_count + 1)
        )
        if r.rowcount != 1:
            return False
        ex = s.get(Execution, execution_id, populate_existing=True)
        s.add(OutboxEvent(execution_id=execution_id, correlation_id=ex.correlation_id,
                          created_at=now, next_attempt_at=now + timedelta(seconds=delay_seconds)))
        self._close_attempt(s, execution_id, "REQUEUED", error.value, None, now)
        return True
