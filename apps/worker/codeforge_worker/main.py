"""Worker entrypoint: consumes execution messages with manual acks and graceful shutdown."""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid

from codeforge_shared import metrics, mq
from codeforge_shared.config import Settings
from codeforge_shared.logging_utils import configure_logging, log_event
from codeforge_shared.store import SqlStore, make_session_factory
from prometheus_client import start_http_server

from .processor import Decision, Processor
from .runner import DockerRunner, reap_orphaned_containers

log = logging.getLogger("codeforge.worker")
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log_event(log, logging.INFO, "shutdown_requested", signal=signum)


def consume_forever(settings: Settings, processor: Processor) -> None:
    while not _stop:
        conn = None
        try:
            conn = mq.connect(settings.rabbitmq_url)
            ch = conn.channel()
            mq.declare_topology(ch, settings.retry_delay_ms)
            ch.confirm_delivery()
            ch.basic_qos(prefetch_count=1)
            log_event(log, logging.INFO, "worker_consuming", worker_id=processor.worker_id)
            last_reap = 0.0
            for method, props, body in ch.consume(mq.QUEUE, inactivity_timeout=1):
                if _stop:
                    break
                now = time.monotonic()
                if now - last_reap > 30.0:
                    last_reap = now
                    reap_orphaned_containers(settings.docker_bin, settings.stale_running_seconds)
                if method is None:
                    continue
                decision = processor.process(body)
                if decision is Decision.RETRY:
                    count = int((props.headers or {}).get(mq.RETRY_HEADER, 0))
                    if count < settings.max_message_retries:
                        mq.publish_retry(ch, body, count + 1)
                    else:
                        mq.publish_dead(ch, body, "retries_exhausted")
                        metrics.MESSAGES.labels("dead_retries_exhausted").inc()
                elif decision is Decision.DEAD:
                    mq.publish_dead(ch, body, "poison")
                # Ack only after state is durable (processor) and any re-publish is confirmed.
                ch.basic_ack(method.delivery_tag)
            if conn.is_open:
                ch.cancel()  # un-acked prefetched message (if any) returns to the queue
        except mq.AMQPError:
            metrics.QUEUE_FAILURES.labels("consume").inc()
            log_event(log, logging.WARNING, "rabbitmq_error_reconnecting", exc_info=True)
            time.sleep(2)
        finally:
            try:
                if conn and conn.is_open:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    start_http_server(settings.worker_metrics_port)
    store = SqlStore(make_session_factory(settings.database_url))
    runner = DockerRunner(settings.limits, settings.docker_bin)
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"[:64]
    consume_forever(settings, Processor(store, runner, settings, worker_id))
    log_event(log, logging.INFO, "worker_stopped", worker_id=worker_id)


if __name__ == "__main__":
    main()
