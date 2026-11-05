"""End-to-end: real PostgreSQL + RabbitMQ, FakeRunner (no Docker needed).
Run with services up:  docker compose up -d postgres rabbitmq
  DATABASE_URL=postgresql+psycopg2://codeforge:codeforge@localhost:5432/codeforge \
  RABBITMQ_URL=amqp://codeforge:codeforge@localhost:5672/%2F python -m pytest tests/integration"""
import os
import time
import unittest

import tests  # noqa: F401
from tests.support.db import HAVE_PIKA, HAVE_SQLALCHEMY

DB, MQ = os.environ.get("DATABASE_URL"), os.environ.get("RABBITMQ_URL")
REASON = ("NOT RUN: needs sqlalchemy+pika installed and DATABASE_URL/RABBITMQ_URL pointing at "
          "running PostgreSQL and RabbitMQ")


@unittest.skipUnless(HAVE_SQLALCHEMY and HAVE_PIKA and DB and MQ, REASON)
class PipelineTests(unittest.TestCase):
    def setUp(self):
        import sqlalchemy as sa
        from codeforge_shared import mq
        from codeforge_shared.config import Settings
        from codeforge_shared.models import Base
        from codeforge_shared.store import SqlStore, make_session_factory
        sf = make_session_factory(DB)
        Base.metadata.create_all(sf.kw["bind"])
        with sf() as s:
            try:
                s.execute(sa.text("TRUNCATE executions, execution_attempts, outbox_events CASCADE"))
                s.commit()
            except Exception:
                s.rollback()
        self.store = SqlStore(sf)
        self.settings = Settings.from_env({})
        self.conn = mq.connect(MQ)
        self.ch = self.conn.channel()
        mq.declare_topology(self.ch, self.settings.retry_delay_ms)
        self.ch.confirm_delivery()
        for q in (mq.QUEUE, mq.RETRY_QUEUE, mq.DEAD_QUEUE):
            self.ch.queue_purge(q)
        self.mq = mq

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _drain_one(self, queue, timeout=5.0):
        """Poll the queue with a deadline to tolerate asynchronous broker routing/requeueing."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            method, props, body = self.ch.basic_get(queue, auto_ack=False)
            if method is not None:
                return method, props, body
            time.sleep(0.05)
        return self.ch.basic_get(queue, auto_ack=False)

    def test_outbox_to_queue_to_result_and_duplicate_delivery(self):
        from codeforge_shared.results import RunResult
        from codeforge_shared.states import ExecutionStatus as S
        from codeforge_worker.processor import Decision, Processor
        from codeforge_worker.publisher import run_once
        from tests.support.fakes import FakeRunner
        ex = self.store.create_execution(language="python", source_code="print(1)", stdin="",
                                         correlation_id="it")
        run_once(self.store, self.settings, lambda e, c: self.mq.publish_execution(self.ch, e, c))
        method, props, body = self._drain_one(self.mq.QUEUE)
        self.assertIsNotNone(method, "Expected message in queue from outbox publish")
        self.assertEqual(props.delivery_mode, 2)  # persistent
        runner = FakeRunner(RunResult(S.SUCCEEDED, 0, "1\n", "", 5))
        p = Processor(self.store, runner, self.settings, "it-worker")
        self.assertIs(p.process(body), Decision.ACK)
        self.ch.basic_ack(method.delivery_tag)
        self.assertEqual(self.store.get_execution(ex.id).status, "SUCCEEDED")
        self.assertIs(p.process(body), Decision.ACK)  # duplicate delivery
        self.assertEqual(runner.calls, 1)

    def test_dead_letter_queue_receives_poison_message(self):
        self.mq.publish_dead(self.ch, b"garbage", "poison")
        method, props, body = self._drain_one(self.mq.DEAD_QUEUE)
        self.assertIsNotNone(method, "Expected poison message in DEAD_QUEUE")
        self.assertEqual(body, b"garbage")
        self.ch.basic_ack(method.delivery_tag)

    def test_rejected_message_is_dead_lettered_by_broker(self):
        self.ch.basic_publish(self.mq.EXCHANGE, self.mq.RK, b"x")
        method, _, _ = self._drain_one(self.mq.QUEUE)
        self.assertIsNotNone(method, "Expected published message in QUEUE")
        self.ch.basic_nack(method.delivery_tag, requeue=False)
        method, _, body = self._drain_one(self.mq.DEAD_QUEUE)
        self.assertIsNotNone(method, "Expected dead-lettered message in DEAD_QUEUE after basic_nack")
        self.assertEqual(body, b"x")
        self.ch.basic_ack(method.delivery_tag)

    def test_unacked_message_is_redelivered_after_worker_death(self):  # window B/C
        self.mq.publish_execution(self.ch, "00000000-0000-0000-0000-000000000001", "c")
        method, _, _ = self._drain_one(self.mq.QUEUE)
        self.assertIsNotNone(method, "Expected published message in QUEUE")
        # simulate crash: close the connection without ack
        self.conn.close()
        self.conn = self.mq.connect(MQ)
        self.ch = self.conn.channel()
        method, _, _ = self._drain_one(self.mq.QUEUE)
        self.assertIsNotNone(method, "Expected redelivered message in QUEUE after connection reset")
        self.assertTrue(method.redelivered)
        self.ch.basic_ack(method.delivery_tag)


if __name__ == "__main__":
    unittest.main()
