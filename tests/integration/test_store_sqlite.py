"""SQL-level tests of SqlStore on in-memory SQLite (state guards, outbox, sweeper).
Requires SQLAlchemy. PostgreSQL-specific behaviour (SKIP LOCKED, real concurrency) is covered by
tests/integration/test_pipeline_postgres_rabbitmq.py."""
import unittest
from datetime import timedelta

import tests  # noqa: F401
from tests.support.db import HAVE_SQLALCHEMY

REASON = "NOT RUN: SQLAlchemy is not installed in this environment"


@unittest.skipUnless(HAVE_SQLALCHEMY, REASON)
class StoreTests(unittest.TestCase):
    def setUp(self):
        from tests.support.db import sqlite_store
        self.store = sqlite_store()

    def new(self):
        return self.store.create_execution(language="python", source_code="print(1)", stdin="",
                                           correlation_id="c1")

    def result(self, status, **kw):
        from codeforge_shared.results import RunResult
        return RunResult(status, kw.get("exit_code", 0), "out", "", 10, kw.get("error_type"))

    def test_create_writes_execution_and_outbox_atomically(self):
        ex = self.new()
        self.assertEqual(ex.status, "QUEUED")
        self.assertEqual(self.store.pending_outbox_count(), 1)

    def test_claim_is_exclusive_and_records_attempt(self):
        from codeforge_shared.results import ClaimKind
        ex = self.new()
        self.assertIs(self.store.claim(ex.id, "w1").kind, ClaimKind.CLAIMED)
        self.assertIs(self.store.claim(ex.id, "w2").kind, ClaimKind.IN_PROGRESS)
        self.assertEqual(self.store.get_execution(ex.id).attempt_count, 1)

    def test_finish_is_idempotent_and_never_overwrites_terminal(self):
        from codeforge_shared.states import ExecutionStatus as S
        ex = self.new()
        self.store.claim(ex.id, "w1")
        self.assertTrue(self.store.finish(ex.id, self.result(S.SUCCEEDED)))
        self.assertFalse(self.store.finish(ex.id, self.result(S.FAILED, exit_code=1)))
        row = self.store.get_execution(ex.id)
        self.assertEqual((row.status, row.exit_code, row.stdout), ("SUCCEEDED", 0, "out"))

    def test_finish_requires_running(self):
        from codeforge_shared.states import ExecutionStatus as S
        ex = self.new()  # still QUEUED
        self.assertFalse(self.store.finish(ex.id, self.result(S.SUCCEEDED)))
        self.assertEqual(self.store.get_execution(ex.id).status, "QUEUED")

    def test_terminal_claim_reports_duplicate(self):
        from codeforge_shared.results import ClaimKind
        from codeforge_shared.states import ExecutionStatus as S
        ex = self.new()
        self.store.claim(ex.id, "w1")
        self.store.finish(ex.id, self.result(S.SUCCEEDED))
        self.assertIs(self.store.claim(ex.id, "w2").kind, ClaimKind.TERMINAL)

    def test_cancel_semantics(self):
        import uuid
        from codeforge_shared.results import CancelOutcome as C
        from codeforge_shared.states import ExecutionStatus as S
        a, b, c = self.new(), self.new(), self.new()
        self.assertIs(self.store.request_cancel(a.id), C.CANCELLED)
        self.assertEqual(self.store.get_execution(a.id).status, "CANCELLED")
        self.store.claim(b.id, "w")
        self.assertIs(self.store.request_cancel(b.id), C.CANCEL_REQUESTED)
        self.assertTrue(self.store.is_cancel_requested(b.id))
        self.assertEqual(self.store.get_execution(b.id).status, "RUNNING")
        self.store.claim(c.id, "w")
        self.store.finish(c.id, self.result(S.SUCCEEDED))
        self.assertIs(self.store.request_cancel(c.id), C.NOT_CANCELLABLE)
        self.assertEqual(self.store.get_execution(c.id).status, "SUCCEEDED")
        self.assertIs(self.store.request_cancel(uuid.uuid4()), C.NOT_FOUND)

    def test_cancelled_queued_execution_cannot_be_claimed(self):
        from codeforge_shared.results import ClaimKind
        ex = self.new()
        self.store.request_cancel(ex.id)
        self.assertIs(self.store.claim(ex.id, "w").kind, ClaimKind.TERMINAL)

    def test_requeue_creates_new_outbox_event(self):
        ex = self.new()
        self.store.claim(ex.id, "w")
        self.store.publish_due_outbox(lambda *_: None, 10)
        self.assertEqual(self.store.pending_outbox_count(), 0)
        self.assertTrue(self.store.requeue(ex.id))
        self.assertEqual(self.store.get_execution(ex.id).status, "QUEUED")
        self.assertEqual(self.store.pending_outbox_count(), 1)

    def test_outbox_recovers_after_publish_failure(self):  # failure window A
        from codeforge_shared.models import utcnow
        ex = self.new()

        def broken(*_):
            raise ConnectionError("rabbitmq down")

        with self.assertRaises(ConnectionError):
            self.store.publish_due_outbox(broken, 10)
        self.assertEqual(self.store.pending_outbox_count(), 1)  # NOT lost, NOT marked published
        sent = []
        # backoff: not due immediately...
        self.assertEqual(self.store.publish_due_outbox(lambda e, c: sent.append(e), 10), 0)
        # ...but published once the broker is back and the backoff elapsed
        n = self.store.publish_due_outbox(lambda e, c: sent.append(e), 10, now=utcnow() + timedelta(minutes=5))
        self.assertEqual((n, sent), (1, [str(ex.id)]))
        self.assertEqual(self.store.pending_outbox_count(), 0)

    def test_sweeper_requeues_stale_running_then_fails_after_max_attempts(self):  # window B
        from codeforge_shared.models import utcnow
        ex = self.new()
        self.store.publish_due_outbox(lambda *_: None, 10)
        self.store.claim(ex.id, "dead-worker")
        later = utcnow() + timedelta(hours=1)
        kw = dict(stale_running_seconds=35, stale_queued_seconds=10**6, max_attempts=2, max_enqueues=9)
        self.assertEqual(self.store.sweep(now=later, **kw)["requeued_running"], 1)
        row = self.store.get_execution(ex.id)
        self.assertEqual(row.status, "QUEUED")
        self.assertEqual(self.store.pending_outbox_count(), 1)
        self.store.claim(ex.id, "dead-worker-2")  # second attempt also lost
        self.assertEqual(self.store.sweep(now=later + timedelta(hours=1), **kw)["failed_running"], 1)
        row = self.store.get_execution(ex.id)
        self.assertEqual((row.status, row.error_type), ("FAILED", "WORKER_LOST"))

    def test_sweeper_does_not_touch_fresh_running_jobs(self):
        ex = self.new()
        self.store.claim(ex.id, "w")
        counts = self.store.sweep(stale_running_seconds=35, stale_queued_seconds=120,
                                  max_attempts=3, max_enqueues=5)
        self.assertEqual(sum(counts.values()), 0)
        self.assertEqual(self.store.get_execution(ex.id).status, "RUNNING")

    def test_sweeper_reenqueues_stranded_queued_then_gives_up(self):  # lost message / DLQ'd
        from codeforge_shared.models import utcnow
        ex = self.new()
        self.store.publish_due_outbox(lambda *_: None, 10)  # published, but message "lost"
        kw = dict(stale_running_seconds=35, stale_queued_seconds=60, max_attempts=3, max_enqueues=2)
        t = utcnow() + timedelta(hours=1)
        self.assertEqual(self.store.sweep(now=t, **kw)["requeued_queued"], 1)
        self.assertEqual(self.store.pending_outbox_count(), 1)
        # the sweeper scheduled the event at simulated time `t`, so publish at that simulated time
        self.store.publish_due_outbox(lambda *_: None, 10, now=t)
        self.assertEqual(self.store.pending_outbox_count(), 0)
        self.assertEqual(self.store.sweep(now=t + timedelta(hours=1), **kw)["failed_queued"], 1)
        self.assertEqual(self.store.get_execution(ex.id).error_type, "QUEUE_EXHAUSTED")

    def test_list_pagination_and_filters(self):
        from codeforge_shared.states import ExecutionStatus as S
        ids = [self.new().id for _ in range(5)]
        self.store.claim(ids[0], "w")
        self.store.finish(ids[0], self.result(S.SUCCEEDED))
        rows, total = self.store.list_executions(page=1, page_size=2)
        self.assertEqual((len(rows), total), (2, 5))
        rows, total = self.store.list_executions(page=3, page_size=2)
        self.assertEqual((len(rows), total), (1, 5))
        _, total = self.store.list_executions(page=1, page_size=10, status="SUCCEEDED")
        self.assertEqual(total, 1)
        _, total = self.store.list_executions(page=1, page_size=10, language="javascript")
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
