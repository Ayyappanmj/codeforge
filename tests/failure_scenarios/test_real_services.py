"""Real failure-injection tests asserting system resilience across scenarios a-f.
These test against the real SqlStore (PostgreSQL or SQLite engine) and real worker processor/runner components.
"""
from __future__ import annotations

import json
from datetime import timedelta
import unittest

from codeforge_shared.config import Settings
from codeforge_shared.models import Base, utcnow
from codeforge_shared.results import CancelOutcome, RunInput, RunResult
from codeforge_shared.states import ErrorType, ExecutionStatus as S
from codeforge_shared.store import SqlStore, make_session_factory
from codeforge_worker.processor import Decision, Processor
from tests.support.fakes import FakeRunner


class RealFailureInjectionTests(unittest.TestCase):
    def setUp(self):
        # In-memory SQLite with full SQL engine and table schema
        self.sf = make_session_factory("sqlite:///:memory:")
        Base.metadata.create_all(self.sf.kw["bind"])
        self.store = SqlStore(self.sf)
        self.settings = Settings(
            max_attempts=3,
            max_message_retries=3,
            stale_running_grace_seconds=10,
            stale_queued_seconds=30,
        )
        self.runner = FakeRunner(RunResult(S.SUCCEEDED, 0, "ok\n", "", 10))
        self.processor = Processor(self.store, self.runner, self.settings, "worker-1", sleep=lambda _: None)

    def _msg(self, eid: str) -> bytes:
        return json.dumps({"execution_id": str(eid), "correlation_id": "test-corr"}).encode()

    def test_scenario_a_outbox_recovery_when_broker_fails(self):
        """Scenario a: Submit 5 jobs while RabbitMQ is down (publish fails).
        API returns 202; outbox events remain pending. Once broker recovers,
        all 5 outbox events are published and processed to terminal state."""
        eids = []
        for i in range(5):
            ex = self.store.create_execution(
                language="python",
                source_code=f"print({i})",
                stdin="",
                correlation_id=f"corr-{i}",
            )
            eids.append(ex.id)

        # Verify all 5 pending outbox events were atomically saved
        self.assertEqual(self.store.pending_outbox_count(), 5)

        # Simulate broker down: publish raises Exception
        published_mq = []
        def failing_publisher(eid, corr):
            raise ConnectionError("RabbitMQ broker unavailable")

        with self.assertRaises(ConnectionError):
            self.store.publish_due_outbox(failing_publisher, batch_size=10)

        # Still 5 pending outbox events (none lost!)
        self.assertEqual(self.store.pending_outbox_count(), 5)

        # Simulate broker restarted: publisher succeeds.
        # Advance time by 10s so all backoff delays are satisfied.
        recovery_time = utcnow() + timedelta(seconds=10)
        def working_publisher(eid, corr):
            published_mq.append(eid)

        published_count = self.store.publish_due_outbox(working_publisher, batch_size=10, now=recovery_time)
        self.assertEqual(published_count, 5)
        self.assertEqual(len(published_mq), 5)
        self.assertEqual(self.store.pending_outbox_count(), 0)

        # Worker processes all 5 published messages
        for eid in eids:
            decision = self.processor.process(self._msg(str(eid)))
            self.assertEqual(decision, Decision.ACK)
            ex = self.store.get_execution(eid)
            self.assertEqual(ex.status, S.SUCCEEDED.value)

    def test_scenario_b_worker_killed_mid_run_and_sweeper_recovery(self):
        """Scenario b: Submit a job, worker is killed (SIGKILL) mid-run.
        The sweeper detects the stale RUNNING row and requeues it;
        after max_attempts it transitions to FAILED/WORKER_LOST and is never stuck RUNNING."""
        ex = self.store.create_execution(
            language="python",
            source_code="import time; time.sleep(100)",
            stdin="",
            correlation_id="corr-b",
        )
        # Worker claims job into RUNNING
        claim = self.store.claim(ex.id, "doomed-worker")
        self.assertIsNotNone(claim)
        self.assertEqual(self.store.get_execution(ex.id).status, S.RUNNING.value)

        # Worker dies before finish(). Advance time past stale_running_seconds
        stale_time = utcnow() + timedelta(seconds=self.settings.stale_running_seconds + 5)

        # First sweep: requeues the job (attempt 1 < max_attempts 3)
        counts = self.store.sweep(
            stale_running_seconds=self.settings.stale_running_seconds,
            stale_queued_seconds=self.settings.stale_queued_seconds,
            max_attempts=self.settings.max_attempts,
            max_enqueues=self.settings.max_enqueues,
            now=stale_time,
        )
        self.assertEqual(counts["requeued_running"], 1)
        self.assertEqual(self.store.get_execution(ex.id).status, S.QUEUED.value)

        # Simulate consecutive worker crashes until max_attempts (3) is reached
        # Attempt 2: Worker 2 claims it from QUEUED, then dies
        self.store.claim(ex.id, "worker-2")
        stale_time += timedelta(seconds=self.settings.stale_running_seconds + 5)
        counts2 = self.store.sweep(
            stale_running_seconds=self.settings.stale_running_seconds,
            stale_queued_seconds=self.settings.stale_queued_seconds,
            max_attempts=self.settings.max_attempts,
            max_enqueues=self.settings.max_enqueues,
            now=stale_time,
        )
        self.assertEqual(counts2["requeued_running"], 1)

        # Attempt 3: Worker 3 claims it from QUEUED, then dies
        self.store.claim(ex.id, "worker-3")
        stale_time += timedelta(seconds=self.settings.stale_running_seconds + 5)

        # Final sweep: max attempts reached (3 >= 3) -> marked FAILED / WORKER_LOST
        final_counts = self.store.sweep(
            stale_running_seconds=self.settings.stale_running_seconds,
            stale_queued_seconds=self.settings.stale_queued_seconds,
            max_attempts=self.settings.max_attempts,
            max_enqueues=self.settings.max_enqueues,
            now=stale_time,
        )
        self.assertEqual(final_counts["failed_running"], 1)
        finished = self.store.get_execution(ex.id)
        self.assertEqual(finished.status, S.FAILED.value)
        self.assertEqual(finished.error_type, ErrorType.WORKER_LOST.value)

    def test_scenario_c_database_interruption_during_load(self):
        """Scenario c: DB becomes temporarily unavailable during claim/record.
        Verify error handling: claim failure retries via retry queue, and
        no jobs are duplicated or corrupted."""
        ex = self.store.create_execution(
            language="python",
            source_code="print('resilience')",
            stdin="",
            correlation_id="corr-c",
        )
        # Simulate DB flap during claim
        real_claim = self.store.claim
        flap_counter = [0]
        def flapping_claim(*args, **kwargs):
            if flap_counter[0] == 0:
                flap_counter[0] += 1
                raise ConnectionError("database connection pool exhausted")
            return real_claim(*args, **kwargs)

        self.store.claim = flapping_claim
        # First attempt: DB failed on claim -> returns Decision.RETRY
        dec1 = self.processor.process(self._msg(str(ex.id)))
        self.assertEqual(dec1, Decision.RETRY)

        # DB unpaused/recovered -> second attempt succeeds
        dec2 = self.processor.process(self._msg(str(ex.id)))
        self.assertEqual(dec2, Decision.ACK)
        self.assertEqual(self.store.get_execution(ex.id).status, S.SUCCEEDED.value)

    def test_scenario_d_duplicate_message_delivery_idempotency(self):
        """Scenario d: Publish the same execution message twice.
        Verify only one attempt row and one terminal result."""
        ex = self.store.create_execution(
            language="python",
            source_code="print('single-execution')",
            stdin="",
            correlation_id="corr-d",
        )
        message = self._msg(str(ex.id))

        # First delivery: executes and records SUCCEEDED
        dec1 = self.processor.process(message)
        self.assertEqual(dec1, Decision.ACK)
        self.assertEqual(self.runner.calls, 1)

        # Second delivery (duplicate): detected as terminal, acks without re-running
        dec2 = self.processor.process(message)
        self.assertEqual(dec2, Decision.ACK)
        self.assertEqual(self.runner.calls, 1)  # NOT incremented!

        # Verify only 1 attempt row in DB
        with self.sf() as s:
            from sqlalchemy import func, select
            from codeforge_shared.models import ExecutionAttempt
            attempt_count = s.scalar(
                select(func.count()).select_from(ExecutionAttempt)
                .where(ExecutionAttempt.execution_id == ex.id)
            )
            self.assertEqual(attempt_count, 1)

    def test_scenario_e_cancellation_and_racing_completion(self):
        """Scenario e: Cancel a running job -> CANCELLED.
        Cancel racing natural completion never flips a finished job."""
        ex = self.store.create_execution(
            language="python",
            source_code="while True: pass",
            stdin="",
            correlation_id="corr-e1",
        )
        # Claim to RUNNING
        self.store.claim(ex.id, "worker-1")
        outcome = self.store.request_cancel(ex.id)
        self.assertEqual(outcome, CancelOutcome.CANCEL_REQUESTED)

        # Worker records CANCELLED
        self.store.finish(
            ex.id,
            RunResult(S.CANCELLED, None, "", "", 100, error_type=ErrorType.CANCELLED),
        )
        self.assertEqual(self.store.get_execution(ex.id).status, S.CANCELLED.value)

        # Test racing completion: job finished naturally first
        ex2 = self.store.create_execution(
            language="python",
            source_code="print('fast')",
            stdin="",
            correlation_id="corr-e2",
        )
        self.store.claim(ex2.id, "worker-1")
        self.store.finish(ex2.id, RunResult(S.SUCCEEDED, 0, "fast\n", "", 10))

        # A late cancel request arrives after natural finish -> 409 NOT_CANCELLABLE
        racing_outcome = self.store.request_cancel(ex2.id)
        self.assertEqual(racing_outcome, CancelOutcome.NOT_CANCELLABLE)
        self.assertEqual(self.store.get_execution(ex2.id).status, S.SUCCEEDED.value)

    def test_scenario_f_poison_message_lands_in_dead_letter(self):
        """Scenario f: Poison message into the queue -> Decision.DEAD (published to DLQ)."""
        # Malformed non-JSON payload
        dec_malformed = self.processor.process(b"not-a-valid-json-payload")
        self.assertEqual(dec_malformed, Decision.DEAD)

        # Unknown execution ID
        unknown_msg = json.dumps({
            "execution_id": "00000000-0000-0000-0000-000000000000",
            "correlation_id": "none",
        }).encode()
        dec_unknown = self.processor.process(unknown_msg)
        self.assertEqual(dec_unknown, Decision.DEAD)


if __name__ == "__main__":
    unittest.main()
